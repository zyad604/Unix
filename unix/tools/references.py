# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Knowing the real thing: reference photos, and the outline of what they show.

A model that builds a real object from memory gets its proportions roughly right and its
details wrong: a feature on the wrong side, a curve replaced by a box. Two tools close that gap
for any subject.

``reference_search`` finds photographs of the real thing on Wikimedia Commons (freely licensed,
no key, no account) and shows them, so the agent looks before it builds and can compare
afterwards with ``match_reference``.

``trace_reference`` reads the silhouette out of one photo (a subject on a plain or transparent
background) and builds it as a solid at real size: the true outline, with its openings, instead
of a guess made of boxes. The agent cuts that solid into the object's parts and details them.

``profile_solid`` makes any single part from an outline the agent draws (in the photo's own
coordinates, or in metres), a thickness and a rounding: a lever, a trigger, a handle, instead of
a box. ``project_reference`` lays the photo itself over the model, lined up by the subject's
outline, so the real surface (texture, markings, wear) is on it. ``outline_numbers`` measures
how well the model's outline and openings match the photo; ``match_reference`` reports it.

Photos are looked at and measured, never put into the scene.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import bpy
from mathutils import Vector
from mathutils.geometry import delaunay_2d_cdt, tessellate_polygon

from . import _turn, assets
from ._util import data_dir
from .registry import ToolError, ToolOutput, registry, tool
from .vision import _IMAGE_EXTS, view_image

COMMONS = "https://commons.wikimedia.org/w/api.php"
ALLOWED_HOSTS = {"commons.wikimedia.org", "upload.wikimedia.org", "thumb.wikimedia.org"}
USER_AGENT = "ChinaUnix/0.1 (Blender add-on; reference photos for 3D modelling)"
MAX_PHOTO_BYTES = 12 * 1024 * 1024
TRACE_SIDE = 640                 # the photo is read at this many pixels along its longer side


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "reference"


def _fetch(url: str, timeout: float = 30.0) -> bytes:
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise ToolError(f"Download host not allowed: {host}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise ToolError("That photo is too large.")
    return data


def _save_credits(folder: str, kept: List[Dict[str, Any]]) -> None:
    """Keep who made each photo and its licence next to it (credits.json): a render that shows a projected photo
    is published with that credit (most Commons photos are CC BY or CC BY-SA)."""
    path = os.path.join(folder, "credits.json")
    try:
        with open(path, encoding="utf-8") as fh:
            credits = json.load(fh)
    except (OSError, ValueError):
        credits = {}
    for item in kept:
        credits[os.path.basename(item["file"])] = {k: item.get(k, "") for k in ("title", "author", "licence", "page")}
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(credits, fh, indent=1)
    except OSError:
        pass


@tool(
    "reference_search",
    """Find photographs of a real thing and SEE them: an object, a vehicle, a building, an
    animal, a plant, a material, a place. Use it before modelling anything that exists in the
    real world, and whenever the user asks for realism or accuracy: you cannot model what you
    have not looked at. Search for the plain name of the exact thing, in two or three words
    ('Glock 17', 'Eames lounge chair', 'red fox'): every word must match, so extra words such
    as 'side view' find less, not more. Returns up to `count` photos from Wikimedia Commons with their file
    paths: study them, read real proportions and details off them, pass a clean side-on one to
    trace_reference to get the true outline, and pass them to match_reference to compare your
    build with the real thing. The photos are for looking at: never place them in the scene.""",
    {
        "query": {"type": "string", "description": "The real thing to look up, as specific as you can."},
        "count": {"type": "integer", "description": "How many photos to return (default 4, max 6)."},
    },
    ["query"],
    main_thread=False,
    timeout=120.0,
    label="Looked up reference photos",
)
def reference_search(query: str, count: int = 4) -> ToolOutput:
    if not assets.settings["enabled"]:
        raise ToolError("Online lookups are turned off in the add-on preferences (Online assets). Work from what you know, "
                        "and from any image the user attached.")
    query = query.strip()
    if not query:
        raise ToolError("Say what to look up.")
    count = max(1, min(int(count), 6))
    words, pages, searched = query.split(), [], query
    while words and not pages:           # every word has to match: when nothing is found, ask for less
        searched = " ".join(words)
        params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": "6",
                  "gsrsearch": f"{searched} filetype:bitmap", "gsrlimit": "16", "prop": "imageinfo",
                  "iiprop": "url|size|mime|extmetadata", "iiurlwidth": "1024", "iiextmetadatafilter": "LicenseShortName|Artist"}
        try:
            found = json.loads(_fetch(COMMONS + "?" + urllib.parse.urlencode(params)).decode("utf-8"))
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"Could not reach Wikimedia Commons ({type(exc).__name__}: {exc}). Work from what you know, and "
                            f"from any image the user attached; say that no reference photos could be fetched.")
        pages = sorted(((found.get("query") or {}).get("pages") or {}).values(), key=lambda page: page.get("index", 0))
        pages = [page for page in pages if (page.get("imageinfo") or [{}])[0].get("mime") in ("image/jpeg", "image/png")]
        words = words[:-1] if len(words) > 2 else []
    folder = data_dir("references", _slug(query))
    kept: List[Dict[str, Any]] = []
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("thumburl") or info.get("url") or ""
        mime = info.get("mime", "")
        if mime not in ("image/jpeg", "image/png") or int(info.get("width") or 0) < 400 or not url:
            continue
        title = re.sub(r"^File:", "", page.get("title", "photo"))
        path = os.path.join(folder, f"{len(kept) + 1}_{_slug(os.path.splitext(title)[0])}{'.png' if mime == 'image/png' else '.jpg'}")
        if not os.path.isfile(path):
            try:
                data = _fetch(url, 60.0)
            except Exception:
                continue
            with open(path, "wb") as handle:
                handle.write(data)
        meta = info.get("extmetadata") or {}
        licence = (meta.get("LicenseShortName") or {}).get("value", "")
        author = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", "")).strip()[:120]
        kept.append({"file": path, "title": title, "licence": licence, "author": author,
                     "page": info.get("descriptionurl", "")})
        if len(kept) >= count:
            break
    _save_credits(folder, kept)
    if not kept:
        raise ToolError(f"No photos found for '{query}'. Try other words: the plain name of the thing, or a broader one.")
    images: List[str] = []
    for index, item in enumerate(kept):
        try:
            preview = registry.run_on_main(lambda path=item["file"]: view_image(path), 60.0)
            images += preview.images[:1]
            item["size"] = preview.data.get("original_size")
            item["image"] = len(images)
        except Exception:
            item["unreadable"] = True
    return ToolOutput({
        "query": query, **({"searched_for": searched} if searched != query else {}), "photos": kept,
        "use": "Images follow in order. Say what you take from them: the outline, the proportions, which side each feature is "
               "on, how surfaces meet, what the materials look like. For the true outline pass a clean side-on photo on a plain "
               "background to trace_reference. To check your build, pass a photo to match_reference from the same angle.",
    }, images)


# ---------------------------------------------------------------------------------------------
# the outline in a photo


_PIXELS: Dict[Tuple[str, float], Any] = {}
_PADDED: Dict[Tuple[str, float], str] = {}


def _pixels(path: str):
    """(H, W, 4) float array of the photo at TRACE_SIDE along its longer side, top row first."""
    import numpy as np

    key = (path, os.path.getmtime(path))
    if key in _PIXELS:
        return _PIXELS[key].copy()
    if len(_PIXELS) > 4:
        _PIXELS.clear()
    _PIXELS[key] = _read_pixels(path)
    return _PIXELS[key].copy()


def _read_pixels(path: str):
    import numpy as np

    image = bpy.data.images.load(path, check_existing=False)
    try:
        width, height = image.size
        if not width or not height:
            raise ToolError(f"Blender could not read the image: {path}")
        factor = TRACE_SIDE / max(width, height)
        if factor < 1.0:
            image.scale(max(8, int(round(width * factor))), max(8, int(round(height * factor))))
        width, height = image.size
        buffer = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(buffer)
    finally:
        bpy.data.images.remove(image)
    return buffer.reshape(height, width, 4)[::-1].copy()


def _padded_photo(path: str, mask, side_px: int = 2048) -> str:
    """A copy of a photo in which the background is painted over with the subject's own colours,
    spreading outward from its edge. A photo laid over a model is sampled right up to the outline
    and a little beyond; with the original, the studio background shows there as a pale fringe."""
    import numpy as np

    key = (path, os.path.getmtime(path))
    if key in _PADDED and os.path.isfile(_PADDED[key]):
        return _PADDED[key]
    image = bpy.data.images.load(path, check_existing=False)
    try:
        width, height = image.size
        factor = side_px / max(width, height)
        if factor < 1.0:
            image.scale(max(8, int(round(width * factor))), max(8, int(round(height * factor))))
        width, height = image.size
        buffer = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(buffer)
    finally:
        bpy.data.images.remove(image)
    full = buffer.reshape(height, width, 4)[::-1]
    rows, columns = mask.shape
    small = _pixels(path)[..., :3]
    # the outermost pixel of the subject is half background: leave it out of what counts as subject
    sure = mask & np.roll(mask, 1, 0) & np.roll(mask, -1, 0) & np.roll(mask, 1, 1) & np.roll(mask, -1, 1)
    if sure.sum() < 16:
        sure = mask
    colour = np.where(sure[..., None], small, 0.0).astype(np.float32)
    known = sure.copy()
    for _pass in range(48):                 # each pass grows the subject's colours one pixel into the background
        if known.all():
            break
        total = np.zeros_like(colour)
        count = np.zeros(known.shape, dtype=np.float32)
        for shift, axis in ((1, 0), (-1, 0), (1, 1), (-1, 1)):
            total += np.roll(colour, shift, axis) * np.roll(known, shift, axis)[..., None]
            count += np.roll(known, shift, axis)
        grow = ~known & (count > 0)
        colour[grow] = total[grow] / count[grow][:, None]
        known |= grow
    colour[~known] = small[sure].mean(axis=0)
    pick_rows = np.minimum((np.arange(height) * rows / height).astype(int), rows - 1)
    pick_columns = np.minimum((np.arange(width) * columns / width).astype(int), columns - 1)
    subject = sure[np.ix_(pick_rows, pick_columns)]
    out = full.copy()
    out[..., :3] = np.where(subject[..., None], full[..., :3], colour[np.ix_(pick_rows, pick_columns)])
    out[..., 3] = 1.0
    target = os.path.join(data_dir("references", "projected"), _slug(os.path.splitext(os.path.basename(path))[0]) + "_surface.png")
    made = bpy.data.images.new("_unix_padded", width, height, alpha=False)
    try:
        made.pixels.foreach_set(np.ascontiguousarray(out[::-1]).ravel())
        made.filepath_raw, made.file_format = target, 'PNG'
        made.save()
    finally:
        bpy.data.images.remove(made)
    _PADDED[key] = target
    return target


def _region(mask, seeds) -> "Any":
    """The cells of ``mask`` reachable from ``seeds`` through cells that share an edge."""
    import numpy as np

    height, width = mask.shape
    reached = np.zeros_like(mask)
    queue = deque()
    for row, column in seeds:
        if mask[row, column] and not reached[row, column]:
            reached[row, column] = True
            queue.append((row, column))
    while queue:
        row, column = queue.popleft()
        for r, c in ((row - 1, column), (row + 1, column), (row, column - 1), (row, column + 1)):
            if 0 <= r < height and 0 <= c < width and mask[r, c] and not reached[r, c]:
                reached[r, c] = True
                queue.append((r, c))
    return reached


def _subject(pixels) -> "Any":
    """Which pixels are the thing and which are background: by transparency when the image has
    it, else by how far a pixel's colour is from the colour around the border."""
    import numpy as np

    height, width = pixels.shape[:2]
    alpha = pixels[..., 3]
    if (alpha < 0.5).mean() > 0.02:
        mask = alpha > 0.5
    else:
        colour = pixels[..., :3]
        frame = np.concatenate([colour[:3].reshape(-1, 3), colour[-3:].reshape(-1, 3), colour[:, :3].reshape(-1, 3),
                                colour[:, -3:].reshape(-1, 3)])
        background = np.median(frame, axis=0)
        spread = float(np.abs(frame - background).max(axis=1).mean())
        if spread > 0.12:
            raise ToolError("The background of that photo is not plain (it varies too much around the edges), so the outline "
                            "cannot be read from it. Pick a photo of the thing on a plain or transparent background.")
        distance = np.abs(colour - background).max(axis=2)
        counts, edges = np.histogram(distance, bins=64, range=(0.0, 1.0))      # Otsu: the split that separates two groups best
        total, levels = counts.sum(), (edges[:-1] + edges[1:]) / 2
        below = np.cumsum(counts)
        weight = np.cumsum(counts * levels)
        between = np.zeros(64)
        valid = (below > 0) & (below < total)
        mean_low = weight[valid] / below[valid]
        mean_high = (weight[-1] - weight[valid]) / (total - below[valid])
        between[valid] = below[valid] * (total - below[valid]) * (mean_low - mean_high) ** 2
        threshold = min(max(float(levels[int(between.argmax())]), spread * 2 + 0.04), 0.5)
        mask = distance > threshold
    cells = np.argwhere(mask)
    if len(cells) < 0.01 * height * width:
        raise ToolError("Nothing stands out from the background in that photo. Pick another one.")
    # the biggest connected piece is the subject
    remaining, best = mask.copy(), None
    for _attempt in range(6):
        seeds = np.argwhere(remaining)
        if not len(seeds):
            break
        piece = _region(remaining, [tuple(seeds[len(seeds) // 2])])
        if best is None or piece.sum() > best.sum():
            best = piece
        remaining &= ~piece
        if best.sum() > remaining.sum():
            break
    mask = best
    # openings: background fully enclosed by the subject. Tiny ones are noise and are closed.
    border = [(0, c) for c in range(width)] + [(height - 1, c) for c in range(width)] + \
             [(r, 0) for r in range(height)] + [(r, width - 1) for r in range(height)]
    outside = _region(~mask, border)
    enclosed = ~mask & ~outside
    minimum = max(12, 0.002 * mask.sum())
    while enclosed.any():
        seed = tuple(np.argwhere(enclosed)[0])
        hole = _region(enclosed, [seed])
        if hole.sum() < minimum:
            mask |= hole
        enclosed &= ~hole
    if mask[0].any() and mask[-1].any() and mask[:, 0].any() and mask[:, -1].any():
        raise ToolError("The subject fills the whole photo, edge to edge, so its outline is cut off. Pick a photo that shows "
                        "all of it with background around.")
    return mask


def _loops(mask) -> List[List[Tuple[float, float]]]:
    """Closed outlines of a mask as lists of corner points (x right, y down): the outer edge
    first, then each opening. Every outline runs with the subject on its right."""
    import numpy as np

    height, width = mask.shape
    padded = np.zeros((height + 2, width + 2), dtype=bool)
    padded[1:-1, 1:-1] = mask
    inside = padded[1:-1, 1:-1]
    steps: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
    for free, start, end in ((~padded[:-2, 1:-1], (0, 0), (1, 0)), (~padded[1:-1, 2:], (1, 0), (1, 1)),
                             (~padded[2:, 1:-1], (1, 1), (0, 1)), (~padded[1:-1, :-2], (0, 1), (0, 0))):
        for row, column in np.argwhere(inside & free):
            steps.setdefault((int(column) + start[0], int(row) + start[1]), []).append((int(column) + end[0], int(row) + end[1]))
    loops = []
    while steps:
        first = next(iter(steps))
        loop, at, heading = [first], first, (0, 0)
        while True:
            options = steps[at]
            if len(options) == 1:
                target = options.pop()
            else:           # two cells touch at this corner only: turn right, so each keeps its own outline
                target = max(options, key=lambda t: heading[0] * (t[1] - at[1]) - heading[1] * (t[0] - at[0]))
                options.remove(target)
            if not options:
                del steps[at]
            heading, at = (target[0] - at[0], target[1] - at[1]), target
            if at == first:
                break
            loop.append(at)
        loops.append(loop)

    def area(loop) -> float:
        return sum(loop[i - 1][0] * loop[i][1] - loop[i][0] * loop[i - 1][1] for i in range(len(loop))) / 2.0

    loops.sort(key=lambda loop: -abs(area(loop)))
    outer_sign = math.copysign(1.0, area(loops[0]))
    return [loops[0]] + [loop for loop in loops[1:] if math.copysign(1.0, area(loop)) != outer_sign and abs(area(loop)) >= 12]


def _smooth(loop: List[Tuple[float, float]], reach: int = 2) -> List[Tuple[float, float]]:
    """The same closed outline with pixel steps and surface grain averaged out: each point moves
    to the weighted middle of its neighbours along the line. Corners soften by about a pixel."""
    count = len(loop)
    if count < 4 * reach + 4:
        return list(loop)
    weights = [math.exp(-0.5 * (k / max(reach * 0.75, 0.5)) ** 2) for k in range(-reach, reach + 1)]
    total = sum(weights)
    out = []
    for i in range(count):
        x = sum(w * loop[(i + k) % count][0] for w, k in zip(weights, range(-reach, reach + 1))) / total
        y = sum(w * loop[(i + k) % count][1] for w, k in zip(weights, range(-reach, reach + 1))) / total
        out.append((x, y))
    return out


def _simplify(loop: List[Tuple[float, float]], tolerance: float) -> List[Tuple[float, float]]:
    """Fewer points along the same outline (Ramer-Douglas-Peucker on a closed loop): pixel
    stair-steps become straight runs and corners stay."""
    if len(loop) < 8:
        return list(loop)
    far = max(range(len(loop)), key=lambda i: (loop[i][0] - loop[0][0]) ** 2 + (loop[i][1] - loop[0][1]) ** 2)

    def reduce(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        keep = [False] * len(points)
        keep[0] = keep[-1] = True
        stack = [(0, len(points) - 1)]
        while stack:
            a, b = stack.pop()
            if b - a < 2:
                continue
            ax, ay = points[a]
            bx, by = points[b]
            dx, dy = bx - ax, by - ay
            length = math.hypot(dx, dy) or 1e-9
            worst, index = 0.0, -1
            for i in range(a + 1, b):
                distance = abs(dx * (points[i][1] - ay) - dy * (points[i][0] - ax)) / length
                if distance > worst:
                    worst, index = distance, i
            if worst > tolerance:
                keep[index] = True
                stack += [(a, index), (index, b)]
        return [point for point, kept in zip(points, keep) if kept]

    return reduce(loop[:far + 1])[:-1] + reduce(loop[far:] + [loop[0]])[:-1]


def _without_slivers(triangles, points: List[Tuple[float, float]]) -> List[Tuple[int, int, int]]:
    """The fill can return a triangle whose three corners lie on one line (an outline has many
    points in a row). It has no area and would leave a crack, so it is taken out and the triangle
    on the other side of its long edge is split at the middle corner instead."""
    triangles = [tuple(t) for t in triangles]

    def doubled_area(t) -> float:
        (ax, ay), (bx, by), (cx, cy) = points[t[0]], points[t[1]], points[t[2]]
        return (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)

    def span(i: int, j: int) -> float:
        return math.hypot(points[i][0] - points[j][0], points[i][1] - points[j][1])

    scale = max(max(p[0] for p in points) - min(p[0] for p in points), max(p[1] for p in points) - min(p[1] for p in points), 1e-12)
    for _round in range(len(triangles)):
        flat = next((t for t in triangles if abs(doubled_area(t)) < 1e-9 * scale * scale), None)
        if flat is None:
            break
        triangles.remove(flat)
        a, b, c = flat
        middle = max(((span(b, c), a), (span(a, c), b), (span(a, b), c)))[1]      # the corner opposite the longest side
        first, last = [corner for corner in flat if corner != middle]
        for index, other in enumerate(triangles):
            if first in other and last in other:
                triangles[index:index + 1] = [tuple(middle if corner == last else corner for corner in other),
                                              tuple(middle if corner == first else corner for corner in other)]
                break
    return triangles


def _solid(name: str, loops: List[List[Tuple[float, float]]], near: float, far: float, round_m: float = 0.0):
    """A closed mesh object from outlines in the X-Z plane (the first is the outer edge, the rest
    are openings), pushed along Y from ``near`` to ``far``. With ``round_m`` its faces curve down
    to the rim instead of meeting it at a corner. Returns (object, triangles in one cap)."""
    import bmesh

    if round_m > 1e-9:
        verts, faces, cap = _rounded(loops, near, far, float(round_m))
    else:
        verts = []
        rings: List[List[int]] = []
        for loop in loops:
            ring = []
            for x, z in loop:
                ring.append(len(verts))
                verts.append((x, near, z))
            rings.append(ring)
        count = len(verts)
        verts += [(x, far, z) for x, _y, z in verts]
        order = [index for ring in rings for index in ring]
        filled = _without_slivers(tessellate_polygon([[Vector((verts[i][0], verts[i][2])) for i in ring] for ring in rings]),
                                  [(verts[i][0], verts[i][2]) for i in order])
        faces = []
        for a, b, c in filled:
            a, b, c = order[a], order[b], order[c]
            faces.append((a, b, c))
            faces.append((c + count, b + count, a + count))
        for ring in rings:
            for k in range(len(ring)):
                a, b = ring[k], ring[(k + 1) % len(ring)]
                faces.append((a, b, b + count, a + count))
        cap = len(filled)
    if not cap:
        raise ToolError("That outline encloses no area. Give at least three points that are not on one line.")
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.validate()
    mesh.update()
    work = bmesh.new()
    work.from_mesh(mesh)
    if round_m <= 1e-9:
        # the fill leaves slivers between points that lie on one line: merge each flat cap back into clean faces
        bmesh.ops.dissolve_limit(work, angle_limit=math.radians(0.5), verts=work.verts[:], edges=work.edges[:])
    bmesh.ops.dissolve_degenerate(work, dist=1e-9, edges=work.edges[:])
    bmesh.ops.recalc_face_normals(work, faces=work.faces)
    work.to_mesh(mesh)
    work.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj, cap


def _without_rim_slivers(triangles, coords, on_rim):
    """Three neighbouring rim points lie almost on one line, and the triangulation joins them into
    a triangle as thin as a hair: sometimes just inside the outline, sometimes just outside, too
    close to tell. Each one is taken out, and if a triangle lay across its long side, that one is
    split at the middle point, so the rim keeps exactly one triangle on every piece of it."""
    kept = list(triangles)

    def sliver(t) -> Optional[Tuple[int, int, int]]:
        if not (on_rim[t[0]] and on_rim[t[1]] and on_rim[t[2]]):
            return None
        (ax, az), (bx, bz), (cx, cz) = coords[t[0]], coords[t[1]], coords[t[2]]
        sides = ((math.hypot(cx - bx, cz - bz), t[0]), (math.hypot(cx - ax, cz - az), t[1]), (math.hypot(bx - ax, bz - az), t[2]))
        longest, middle = max(sides)
        if abs((bx - ax) * (cz - az) - (cx - ax) * (bz - az)) >= 2e-3 * longest * longest:
            return None
        first, last = [corner for corner in t if corner != middle]
        return middle, first, last

    for _pass in range(12):              # splitting a neighbour can leave another sliver: go on until none is left
        found = [(t, parts) for t, parts in ((t, sliver(t)) for t in kept) if parts is not None]
        if not found:
            break
        for flat, (middle, first, last) in found:
            if flat not in kept:
                continue
            kept.remove(flat)
            for index, other in enumerate(kept):
                if first in other and last in other:
                    kept[index:index + 1] = [tuple(middle if corner == last else corner for corner in other),
                                             tuple(middle if corner == first else corner for corner in other)]
                    break
    return kept


def _segments(loops):
    import numpy as np

    starts = np.array([point for loop in loops for point in loop], dtype=np.float64)
    ends = np.array([loop[(k + 1) % len(loop)] for loop in loops for k in range(len(loop))], dtype=np.float64)
    return starts, ends


def _to_outline(points, starts, ends):
    """Distance from each point to the nearest piece of outline."""
    import numpy as np

    out = np.empty(len(points))
    along = ends - starts
    squared = np.maximum((along ** 2).sum(axis=1), 1e-30)
    for first in range(0, len(points), 1500):
        chunk = points[first:first + 1500]
        offset = chunk[:, None, :] - starts[None, :, :]
        t = np.clip((offset * along[None, :, :]).sum(axis=2) / squared[None, :], 0.0, 1.0)
        nearest = starts[None, :, :] + t[:, :, None] * along[None, :, :]
        out[first:first + 1500] = np.sqrt(((chunk[:, None, :] - nearest) ** 2).sum(axis=2)).min(axis=1)
    return out


def _enclosed(points, starts, ends):
    """Which points lie inside the outlines (inside the outer one and not in an opening)."""
    import numpy as np

    out = np.zeros(len(points), dtype=bool)
    for first in range(0, len(points), 1500):
        chunk = points[first:first + 1500]
        x, z = chunk[:, 0][:, None], chunk[:, 1][:, None]
        z0, z1, x0, x1 = starts[None, :, 1], ends[None, :, 1], starts[None, :, 0], ends[None, :, 0]
        crosses = (z0 > z) != (z1 > z)
        with np.errstate(divide="ignore", invalid="ignore"):
            at = x0 + (z - z0) * (x1 - x0) / (z1 - z0)
        out[first:first + 1500] = ((crosses & (x < at)).sum(axis=1) % 2) == 1
    return out


def _rounded(loops: List[List[Tuple[float, float]]], near: float, far: float, reach: float):
    """Vertices and faces of a solid whose two faces curve down to the rim over ``reach``: a
    quarter circle when the rounding fits in the thickness, an ellipse (a pillow, no flat rim
    wall) when it is larger. A stretch of the shape narrower than twice the reach never gets to
    full thickness, so thin features come out as rods and blades, not as slabs."""
    import numpy as np

    half, middle = (far - near) / 2, (near + far) / 2
    drop = min(reach, half)
    wall = half - drop
    points = np.array([p for loop in loops for p in loop], dtype=np.float64)
    low, high = points.min(axis=0), points.max(axis=0)
    size = float(max(high - low))

    def lattice(step: float):
        columns = np.arange(low[0] + step / 2, high[0], step)
        rows = np.arange(low[1] + step / 2, high[1], step)
        if not len(columns) or not len(rows):
            return np.zeros((0, 2))
        grid_x, grid_z = np.meshgrid(columns, rows)
        grid_x = grid_x + (np.arange(len(rows)) % 2)[:, None] * step / 2      # every other row shifted: even triangles
        return np.stack([grid_x.ravel(), grid_z.ravel()], axis=1)

    fine = max(reach / 7.0, size / 300.0)           # seven steps across the curve, unless that is too many points
    for _attempt in range(10):
        coarse = max(fine * 4.0, size / 36.0)
        dense = []
        for loop in loops:                          # the rim itself needs points as close as the lattice next to it
            ring = []
            for k in range(len(loop)):
                (ax, az), (bx, bz) = loop[k], loop[(k + 1) % len(loop)]
                pieces = max(1, int(math.ceil(math.hypot(bx - ax, bz - az) / (fine * 1.5))))
                ring += [(ax + (bx - ax) * i / pieces, az + (bz - az) * i / pieces) for i in range(pieces)]
            dense.append(ring)
        starts, ends = _segments(dense)
        near_rim = lattice(fine)
        if len(near_rim):
            away = _to_outline(near_rim, starts, ends)
            near_rim = near_rim[_enclosed(near_rim, starts, ends) & (away > 0.45 * fine) & (away < reach + fine)]
        flat = lattice(coarse)
        if len(flat):
            away = _to_outline(flat, starts, ends)
            flat = flat[_enclosed(flat, starts, ends) & (away >= reach + fine)]
        if len(near_rim) + len(flat) + len(starts) <= 1600:
            break
        fine *= 1.25
    rim_count = len(starts)
    everything = np.concatenate([starts, near_rim, flat])
    edges, at = [], 0
    for ring in dense:
        edges += [(at + k, at + (k + 1) % len(ring)) for k in range(len(ring))]
        at += len(ring)
    result = delaunay_2d_cdt([Vector(point) for point in everything], edges, [], 0, 1e-9 * size)
    coords = np.array([tuple(v) for v in result[0]], dtype=np.float64)
    away = _to_outline(coords, starts, ends)
    on_rim = away < 1e-7 * size
    triangles = _without_rim_slivers([tuple(face) for face in result[2] if len(face) == 3], coords, on_rim)
    centres = np.array([coords[list(t)].mean(axis=0) for t in triangles]) if triangles else np.zeros((0, 2))
    keep = _enclosed(centres, starts, ends) if len(centres) else []
    triangles = [t for t, inside in zip(triangles, keep) if inside]
    rim_edges = [tuple(edge) for edge, source in zip(result[1], result[4]) if source]
    rise = wall + drop * np.sqrt(np.clip(1.0 - (1.0 - np.minimum(away, reach) / reach) ** 2, 0.0, 1.0))
    count = len(coords)
    verts = [(float(x), middle - float(h), float(z)) for (x, z), h in zip(coords, rise)]
    back = list(range(count))
    for index in range(count):
        if wall > 1e-9 or not on_rim[index]:        # with no rim wall, the two faces meet in one edge
            back[index] = len(verts)
            verts.append((float(coords[index][0]), middle + float(rise[index]), float(coords[index][1])))
    faces: List[Tuple[int, ...]] = []
    for a, b, c in triangles:
        faces.append((a, b, c))
        if len({back[a], back[b], back[c]}) == 3:
            faces.append((back[c], back[b], back[a]))
    if wall > 1e-9:
        faces += [(a, b, back[b], back[a]) for a, b in rim_edges]
    return verts, faces, len(triangles)


@tool(
    "trace_reference",
    """Build the TRUE OUTLINE of a real thing from a photo of it: reads the silhouette (with
    its openings: a trigger guard, a handle, the gap between chair legs) from a photo taken
    square-on against a plain or transparent background, and makes it a solid of the real size
    and thickness, standing on the ground plane and facing the front view (X right, Z up).
    This is how a realistic object starts: from its real outline, not from boxes. Then cut the
    solid into the object's real parts (bisect it along the lines where parts meet), give each
    its own thickness and rounding, and add the details. `look` with view 'front' shows it the
    way the photo does; match_reference with the same photo and view 'front' shows how close
    you are. Returns a picture of what was read from the photo: check that it is the whole
    subject and nothing else before building on it.""",
    {
        "image": {"type": "string", "description": "Path of a photo showing the thing square-on (side, front or top) on a plain or transparent background."},
        "name": {"type": "string", "description": "Name for the new object."},
        "length_m": {"type": "number", "description": "The real size of the thing along its longest side in the photo, in meters."},
        "thickness_m": {"type": "number", "description": "Its real thickness front to back, in meters (default: 12% of the length)."},
        "mirror": {"type": "boolean", "description": "Flip left and right, when the photo shows the opposite side to the one you want facing the front view."},
        "detail": {"type": "number", "description": "How closely the outline follows the photo, in pixels of the 640 px trace (default 0.8; lower keeps more points)."},
        "round_m": {"type": "number", "description": "Curve the faces down to the rim over this distance, in meters (default 0: a flat measuring solid to build the parts against; for a thing made of one piece, give it its real rounding here)."},
    },
    ["image", "name", "length_m"],
    mutates=True,
    timeout=120.0,
    label="Traced the outline from a photo",
)
def trace_reference(image: str, name: str, length_m: float, thickness_m: Optional[float] = None,
                    mirror: bool = False, detail: float = 0.8, round_m: float = 0.0) -> ToolOutput:
    import numpy as np

    path = bpy.path.abspath(image)
    if not os.path.isfile(path) or os.path.splitext(path)[1].lower() not in _IMAGE_EXTS:
        raise ToolError(f"Not an image file: {image}")
    if not 0.0005 <= float(length_m) <= 5000.0:
        raise ToolError("length_m is the real size in meters (a pistol is about 0.2, a car about 4.5).")
    pixels = _pixels(path)
    mask = _subject(pixels)
    if mirror:
        mask, pixels = mask[:, ::-1], pixels[:, ::-1]
    loops = [_simplify(_smooth(loop), max(0.3, min(float(detail), 6.0))) for loop in _loops(mask)]
    loops = [loop for loop in loops if len(loop) >= 3]
    outer = loops[0]
    xs, ys = [p[0] for p in outer], [p[1] for p in outer]
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    scale = float(length_m) / span
    middle, floor = (min(xs) + max(xs)) / 2, max(ys)
    thickness = float(thickness_m) if thickness_m else 0.12 * float(length_m)
    half = thickness / 2

    _turn.mark_mutation()
    loops_m = [[((x - middle) * scale, (floor - y) * scale) for x, y in loop] for loop in loops]
    obj, filled = _solid(name, loops_m, -half, half, max(0.0, float(round_m or 0.0)))
    if round_m:
        for poly in obj.data.polygons:
            poly.use_smooth = True
    _MASKS[(path, bool(mirror))] = mask
    height_px, width_px = mask.shape
    # where the photo sits in the scene, so positions read off the photo can be turned into metres
    obj["unix_photo"] = path
    obj["unix_photo_frame"] = [(0 - middle) * scale, floor * scale, width_px * scale, height_px * scale, 1.0 if mirror else 0.0]

    # what was read from the photo: the subject as it is, the background dimmed, the outline drawn
    shown = pixels[..., :3] * np.where(mask, 1.0, 0.25)[..., None]
    edge = mask & ~(np.roll(mask, 1, 0) & np.roll(mask, -1, 0) & np.roll(mask, 1, 1) & np.roll(mask, -1, 1))
    shown[edge] = (1.0, 0.45, 0.0)
    height, width = mask.shape
    preview = bpy.data.images.new("_unix_trace", width, height, alpha=False)
    out = os.path.join(data_dir("looks"), f"trace_{time.strftime('%H%M%S')}.jpg")
    try:
        rgba = np.concatenate([shown[::-1], np.ones((height, width, 1), dtype=np.float32)], axis=2).astype(np.float32)
        preview.pixels.foreach_set(np.ascontiguousarray(rgba).ravel())
        preview.filepath_raw, preview.file_format = out, 'JPEG'
        preview.save()
    finally:
        bpy.data.images.remove(preview)

    size = [round((max(xs) - min(xs)) * scale, 4), round(thickness, 4), round((max(ys) - min(ys)) * scale, 4)]
    return ToolOutput({
        "object": obj.name, "size_m": size, "outline_points": len(outer), "openings": len(loops) - 1,
        "triangles": filled * 2 + sum(len(loop) for loop in loops) * 2,
        "photo_frame": "A point at fraction (u, v) of the photo, u from its left edge and v from its top edge, both 0 to 1, "
                       "lies at X = {:.4f} + u * {:.4f} and Z = {:.4f} - v * {:.4f} (meters). profile_solid takes points in "
                       "these photo fractions directly.".format(*obj["unix_photo_frame"][:1], obj["unix_photo_frame"][2],
                                                               obj["unix_photo_frame"][1], obj["unix_photo_frame"][3]),
        "facing": "the photo's view is the 'front' view: X is right, Z is up, the solid runs along Y",
        "read_from_photo": "The image shows what was taken as the subject (bright, outlined in orange) and what was taken as "
                           "background (dimmed). If a shadow, a stand or a second object was included, or part of the subject "
                           "is missing, remove this object and trace another photo.",
        "next": "This solid is the true outline: keep it, hidden, to measure against. Build every part with profile_solid in "
                "photo fractions: for each big part draw a loose region around it that runs outside the silhouette (it then "
                "follows this outline exactly), with that part's real thickness and rounding; then the small parts. Check with "
                "look_around (all six sides) and match_reference(view='front') with this photo, which measures the outline and "
                "the openings. When the form is right, project_reference puts the photo's real surface on it.",
    }, [out])


# ---------------------------------------------------------------------------------------------
# parts from drawn profiles


_MASKS: Dict[Tuple[str, bool], Any] = {}


def _photo_mask(path: str, mirror: bool):
    """The subject of a traced photo, as trace_reference read it."""
    key = (path, bool(mirror))
    if key not in _MASKS:
        if len(_MASKS) > 6:
            _MASKS.clear()
        mask = _subject(_pixels(path))
        _MASKS[key] = mask[:, ::-1] if mirror else mask
    return _MASKS[key]


def _inside_polygon(shape: Tuple[int, int], polygon: List[Tuple[float, float]]):
    """Which pixels of an image lie inside a polygon given in pixel coordinates."""
    import numpy as np

    rows, columns = shape
    x = (np.arange(columns) + 0.5)[None, :]
    z = (np.arange(rows) + 0.5)[:, None]
    inside = np.zeros(shape, dtype=bool)
    for k in range(len(polygon)):
        (x0, z0), (x1, z1) = polygon[k], polygon[(k + 1) % len(polygon)]
        if z0 == z1:
            continue
        crosses = (z0 > z) != (z1 > z)
        at = x0 + (z - z0) * (x1 - x0) / (z1 - z0)
        inside ^= crosses & (x < at)
    return inside


def _traced(name: str = "") -> bpy.types.Object:
    """The traced solid whose photo frame positions are measured in."""
    if name:
        obj = bpy.data.objects.get(name)
        if obj is None or "unix_photo_frame" not in obj:
            raise ToolError(f"'{name}' was not made by trace_reference.")
        return obj
    found = [o for o in bpy.data.objects if "unix_photo_frame" in o]
    if not found:
        raise ToolError("Photo fractions need a traced photo: call trace_reference first, or pass points in meters with "
                        "space='scene'.")
    return found[-1]


def _own_surface(obj: bpy.types.Object, source: bpy.types.Object, base: Optional[bpy.types.Material] = None) -> bool:
    """Give a part that was placed by the traced photo that photo's surface: its colour, markings
    and wear where the part is, on both of its sides, with the photo's tone on the faces between.
    A part is never left as a plain block next to textured ones. False when the photo shows
    nothing where the part is (an added accessory)."""
    import numpy as np

    path = source["unix_photo"]
    if not os.path.isfile(path):
        return False
    left, top, width, height, mirrored = source["unix_photo_frame"]
    shown = _photo_mask(path, bool(mirrored))
    plain = shown[:, ::-1] if mirrored else shown
    rows, columns = plain.shape
    mesh = obj.data
    world = obj.matrix_world
    placed = [world @ v.co for v in mesh.vertices]
    us = np.array([(at.x - left) / width for at in placed])
    if mirrored:
        us = 1.0 - us
    vs = np.array([(top - at.z) / height for at in placed])
    column0, column1 = int(max(us.min() * columns, 0)), int(min(math.ceil(us.max() * columns), columns))
    row0, row1 = int(max(vs.min() * rows, 0)), int(min(math.ceil(vs.max() * rows), rows))
    window = plain[row0:row1, column0:column1]
    if window.size == 0 or window.sum() < max(4, 0.25 * window.size):
        return False                    # mostly background there: the part is not on the photo
    seen = _pixels(path)[row0:row1, column0:column1][window][:, :3].mean(axis=0)
    linear = np.where(seen <= 0.04045, seen / 12.92, ((seen + 0.055) / 1.055) ** 2.4)
    tone = (float(linear[0]), float(linear[1]), float(linear[2]), 1.0)
    layer = mesh.uv_layers.get("UnixPhoto") or mesh.uv_layers.new(name="UnixPhoto")
    for loop in mesh.loops:
        layer.data[loop.index].uv = (float(us[loop.vertex_index]), 1.0 - float(vs[loop.vertex_index]))
    mesh.uv_layers.active = layer
    layer.active_render = True
    picture = bpy.data.images.load(_padded_photo(path, plain), check_existing=True)
    material = base.copy() if base is not None else bpy.data.materials.new(obj.name + "_Surface")
    material.name = obj.name + "_Surface"
    material.use_nodes = True
    if base is None:
        shader = next((n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if shader is not None:
            shader.inputs["Roughness"].default_value = 0.55
    material.diffuse_color = tone
    material["unix_photo_base"] = base.name if base is not None else ""
    mesh.materials.clear()
    mesh.materials.append(material)
    _photo_layer(material, picture, "UnixPhoto", "both", 0.15, tone)
    return True


def _rounder(points: List[Tuple[float, float]], passes: int) -> List[Tuple[float, float]]:
    """Corner cutting (Chaikin): a few drawn points become a smooth closed curve."""
    for _pass in range(passes):
        out = []
        for k in range(len(points)):
            (ax, ay), (bx, by) = points[k], points[(k + 1) % len(points)]
            out += [(ax * 0.75 + bx * 0.25, ay * 0.75 + by * 0.25), (ax * 0.25 + bx * 0.75, ay * 0.25 + by * 0.75)]
        points = out
    return points


@tool(
    "profile_solid",
    """Make ONE PART from its outline: a lever, a trigger, a handle, a button, a blade, a
    bracket, a sight, a fin. Give the outline as points, the part's thickness and how round its
    edges are, and you get a closed solid with a rounded cross-section, never a box. Use it for
    EVERY part of a realistic object, big and small. Points are read off the reference photo:
    with space 'photo' they are fractions (u, v) of the traced photo, u from its left edge and v
    from its top edge. For a big part (a slide, a frame, a grip, a seat, a lid) draw a loose
    region around it that runs OUTSIDE the silhouette: the part then follows the real outline
    exactly, and your lines are used only where it meets the next part. For a small part draw
    its own outline. With space 'scene' points are (x, z) in meters. The outline lies in the
    front view (X right, Z up); the part is centred at `y_m` along the depth, and the side
    facing the front view's viewer is -Y. `round_m` is how far the faces curve down to the rim:
    a fifth of the thickness softens the edges, half the thickness or more makes an oval
    cross-section (a grip, a handle, a limb), and a narrow stretch comes out thinner by itself.
    Give real things real rounding: seen from its end, a slab with soft edges still reads as a box.""",
    {
        "name": {"type": "string"},
        "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
                   "description": "The outline, in order around the part: [[u, v], ...] photo fractions, or [[x, z], ...] meters."},
        "thickness_m": {"type": "number", "description": "The part's thickness along the depth, in meters."},
        "space": {"type": "string", "enum": ["photo", "scene"], "description": "What the points are (default photo)."},
        "trace": {"type": "string", "description": "The traced object whose photo the fractions refer to (default: the latest)."},
        "y_m": {"type": "number", "description": "Where the part's middle sits along the depth (default 0, the middle of a traced solid; negative is toward the front-view viewer)."},
        "round_m": {"type": "number", "description": "How far in from the rim the faces curve, in meters (default: 35% of the thickness; 0 for sharp edges; half the thickness or more for an oval section)."},
        "smooth": {"type": "integer", "description": "0 keeps the corners you gave, 1-3 turn them into a curve (default 1). Ignored where the part follows the real outline."},
        "free": {"type": "boolean", "description": "Let the part reach outside the traced outline (an added accessory). Default false: in photo space a part never sticks out of the real silhouette."},
        "holes": {"type": "array", "items": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                  "description": "Optional openings in the part, each an outline in the same space."},
        "material": {"type": "string", "description": "Name of an existing material whose settings (roughness, metal) the part takes."},
        "surface": {"type": "string", "enum": ["photo", "plain"], "description": "photo (default in photo space): the part carries the traced photo's own colour, markings and wear where it sits. plain: only the material."},
        "width_profile": {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
                          "description": "How the thickness changes along the part: [[0, 1.0], [1, 0.6]] tapers it to 60% from one end to the other; 0 is the left (or bottom) end. Real parts are rarely one thickness along their length."},
        "width_axis": {"type": "string", "enum": ["x", "z"], "description": "The direction width_profile runs along: x (left to right, default) or z (bottom to top)."},
    },
    ["name", "points", "thickness_m"],
    mutates=True,
    label="Made a part from its outline",
)
def profile_solid(name: str, points: List[List[float]], thickness_m: float, space: str = "photo", trace: str = "",
                  y_m: float = 0.0, round_m: Optional[float] = None, smooth: int = 1,
                  holes: Optional[List[List[List[float]]]] = None, material: str = "", free: bool = False,
                  surface: str = "photo", width_profile: Optional[List[List[float]]] = None, width_axis: str = "x") -> Dict[str, Any]:
    if space not in ("photo", "scene"):
        raise ToolError("space is 'photo' or 'scene'.")
    source = None
    thickness = float(thickness_m)
    if thickness <= 0:
        raise ToolError("thickness_m must be more than zero.")
    outlines = [points] + list(holes or [])
    if any(len(outline) < 3 or any(len(point) < 2 for point in outline) for outline in outlines):
        raise ToolError("Every outline needs at least three [a, b] points.")
    followed = False
    passes = max(0, min(int(smooth), 3))
    if space == "photo":
        source = _traced(trace)
        left, top, width, height, mirrored = source["unix_photo_frame"]
        if any(not (-0.5 <= point[0] <= 1.5 and -0.5 <= point[1] <= 1.5) for outline in outlines for point in outline):
            raise ToolError("Photo fractions run from 0 to 1 (u from the left edge, v from the top edge). For meters use space='scene'.")
        if not free:                    # where the drawn region leaves the silhouette, the real outline takes over
            mask = _photo_mask(source["unix_photo"], bool(mirrored))
            rows, columns = mask.shape
            region = _inside_polygon(mask.shape, [(float(u) * columns, float(v) * rows) for u, v, *_rest in outlines[0]])
            for hole in outlines[1:]:
                region &= ~_inside_polygon(mask.shape, [(float(u) * columns, float(v) * rows) for u, v, *_rest in hole])
            part = region & mask
            if (region & ~mask).sum() > max(12, 0.01 * region.sum()):
                if part.sum() < 12:
                    raise ToolError("Those points lie outside the subject of the traced photo. Photo fractions run from the "
                                    "photo's left and top edges; check them against the picture, or pass free=true for a part "
                                    "that is not on the photo.")
                seeds = [tuple(cell) for cell in __import__("numpy").argwhere(part)]
                piece = _region(part, [seeds[len(seeds) // 2]])
                if piece.sum() < 0.6 * part.sum():          # the middle seed hit a small island: take the biggest instead
                    rest = part & ~piece
                    other = _region(rest, [tuple(__import__("numpy").argwhere(rest)[0])])
                    piece = other if other.sum() > piece.sum() else piece
                traced_loops = [_simplify(_smooth(loop), 0.8) for loop in _loops(piece)]
                outlines = [[(left + x / columns * width, top - z / rows * height) for x, z in loop]
                            for loop in traced_loops if len(loop) >= 3]
                followed = True
        if not followed:
            outlines = [_rounder([(left + float(u) * width, top - float(v) * height) for u, v, *_rest in outline], passes)
                        for outline in outlines]
    else:
        outlines = [_rounder([(float(x), float(z)) for x, z, *_rest in outline], passes) for outline in outlines]
    target = bpy.data.materials.get(material) if material else None
    if material and target is None:
        raise ToolError(f"No material named '{material}'.")
    _turn.mark_mutation()
    radius = 0.35 * thickness if round_m is None else max(0.0, float(round_m))
    obj, _filled = _solid(name, outlines, float(y_m) - thickness / 2, float(y_m) + thickness / 2, radius)
    if radius > 0:
        for poly in obj.data.polygons:
            poly.use_smooth = True
    xs, zs = [p[0] for p in outlines[0]], [p[1] for p in outlines[0]]
    if width_profile:                   # thicker here, thinner there: seen from above, a real part is not a ruler
        stations = sorted((max(0.0, min(1.0, float(a))), max(0.02, float(f))) for a, f, *_rest in width_profile)
        low, span = (min(xs), max(max(xs) - min(xs), 1e-9)) if width_axis != "z" else (min(zs), max(max(zs) - min(zs), 1e-9))
        for vertex in obj.data.vertices:
            along = ((vertex.co.x if width_axis != "z" else vertex.co.z) - low) / span
            factor = stations[0][1] if along <= stations[0][0] else stations[-1][1]
            for (a0, f0), (a1, f1) in zip(stations, stations[1:]):
                if a0 <= along <= a1:
                    factor = f0 + (f1 - f0) * (along - a0) / max(a1 - a0, 1e-9)
                    break
            vertex.co.y = float(y_m) + (vertex.co.y - float(y_m)) * factor
        obj.data.update()
    textured = False
    if source is not None and surface != "plain":
        textured = _own_surface(obj, source, target)
    if not textured and target is not None:
        obj.data.materials.append(target)
    bpy.context.view_layer.update()
    return {"object": obj.name, "size_m": [round(max(xs) - min(xs), 4), round(thickness, 4), round(max(zs) - min(zs), 4)],
            **({"surface": "the traced photo's own surface, on both sides"} if textured else {}),
            "centre_m": [round((min(xs) + max(xs)) / 2, 4), round(float(y_m), 4), round((min(zs) + max(zs)) / 2, 4)],
            "outline_points": len(outlines[0]), "openings": len(outlines) - 1, "rounding_m": round(radius, 5),
            "triangles": sum(len(p.vertices) - 2 for p in obj.data.polygons),
            **({"followed_real_outline": "Where your region ran outside the silhouette the part follows the real outline; your "
                                         "lines are its edges only where it meets other parts."} if followed else {}),
            "next": "look_around shows it from all six sides. From its end it must not read as a box: raise round_m if it does. "
                    "If it sits inside another part, move it toward the viewer (smaller y_m) until it shows."}


@tool(
    "lathe_solid",
    """Make a ROUND part: a barrel, a tube, a pipe, a rod, a leg, a handle, a wheel, a lens, a
    bottle, a knob. Give the two ends of its axis and its radius, and you get a true cylinder
    between them; `radii` changes the radius along the way (a taper, a step, a flare, a ring),
    and `bore_m` makes it hollow: if the real thing has a hole down the middle (a barrel, a pipe,
    a tube, a ring), give it one, because a solid rod where a hole should be is plainly wrong.
    The ends are read off the reference photo as fractions (u, v) with space 'photo', or given
    as (x, z) in meters with space 'scene'; the axis lies in the front view and the part is
    centred at `y_m` along the depth.""",
    {
        "name": {"type": "string"},
        "start": {"type": "array", "items": {"type": "number"}, "description": "One end of the axis: [u, v] photo fractions, or [x, z] meters."},
        "end": {"type": "array", "items": {"type": "number"}, "description": "The other end of the axis."},
        "radius_m": {"type": "number", "description": "Outer radius in meters."},
        "bore_m": {"type": "number", "description": "Radius of the hole through it, in meters (default 0: solid)."},
        "radii": {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
                  "description": "Radius along the axis: [[0, 0.008], [0.9, 0.008], [0.9, 0.011], [1, 0.011]] is a tube with a wider collar at its end; 0 is `start`."},
        "space": {"type": "string", "enum": ["photo", "scene"], "description": "What start and end are (default photo)."},
        "trace": {"type": "string", "description": "The traced object whose photo the fractions refer to (default: the latest)."},
        "y_m": {"type": "number", "description": "Where the axis sits along the depth (default 0)."},
        "segments": {"type": "integer", "description": "Sides around (default 28)."},
        "material": {"type": "string", "description": "Name of an existing material whose settings the part takes."},
        "surface": {"type": "string", "enum": ["photo", "plain"], "description": "photo (default in photo space): carries the traced photo's surface where it sits. plain: only the material."},
    },
    ["name", "start", "end", "radius_m"],
    mutates=True,
    label="Made a round part",
)
def lathe_solid(name: str, start: List[float], end: List[float], radius_m: float, bore_m: float = 0.0,
                radii: Optional[List[List[float]]] = None, space: str = "photo", trace: str = "", y_m: float = 0.0,
                segments: int = 28, material: str = "", surface: str = "photo") -> Dict[str, Any]:
    import bmesh

    if space not in ("photo", "scene"):
        raise ToolError("space is 'photo' or 'scene'.")
    if len(start) < 2 or len(end) < 2:
        raise ToolError("start and end are [a, b] pairs.")
    source = None
    if space == "photo":
        source = _traced(trace)
        left, top, width, height, _mirrored = source["unix_photo_frame"]
        first = Vector((left + float(start[0]) * width, float(y_m), top - float(start[1]) * height))
        last = Vector((left + float(end[0]) * width, float(y_m), top - float(end[1]) * height))
    else:
        first, last = Vector((float(start[0]), float(y_m), float(start[1]))), Vector((float(end[0]), float(y_m), float(end[1])))
    axis = last - first
    length = axis.length
    if length < 1e-6:
        raise ToolError("start and end are the same point.")
    stations = sorted((max(0.0, min(1.0, float(a))), float(r)) for a, r, *_rest in radii) if radii \
        else [(0.0, float(radius_m)), (1.0, float(radius_m))]
    if stations[0][0] > 0.0:
        stations.insert(0, (0.0, stations[0][1]))
    if stations[-1][0] < 1.0:
        stations.append((1.0, stations[-1][1]))
    smallest = min(r for _a, r in stations)
    bore = max(0.0, float(bore_m))
    if smallest <= 0:
        raise ToolError("Every radius must be more than zero.")
    if bore >= smallest:
        raise ToolError(f"bore_m {bore} must be smaller than the smallest outer radius {smallest}.")
    target = bpy.data.materials.get(material) if material else None
    if material and target is None:
        raise ToolError(f"No material named '{material}'.")
    sides = max(8, min(int(segments), 96))
    unit = axis / length
    across = Vector((0.0, 1.0, 0.0))
    other = unit.cross(across).normalized()
    _turn.mark_mutation()
    verts: List[Tuple[float, float, float]] = []
    faces: List[Tuple[int, ...]] = []
    flat: List[int] = []

    def ring(along: float, radius: float) -> List[int]:
        centre = first + unit * (along * length)
        made = []
        for k in range(sides):
            angle = 2 * math.pi * k / sides
            made.append(len(verts))
            verts.append(tuple(centre + (across * math.cos(angle) + other * math.sin(angle)) * radius))
        return made

    outer = [ring(a, r) for a, r in stations]
    for a, b in zip(outer, outer[1:]):
        faces += [(a[k], a[(k + 1) % sides], b[(k + 1) % sides], b[k]) for k in range(sides)]
    if bore > 0:
        inner = [ring(0.0, bore), ring(1.0, bore)]
        faces += [(inner[0][k], inner[1][k], inner[1][(k + 1) % sides], inner[0][(k + 1) % sides]) for k in range(sides)]
        for rim, hole in ((outer[0], inner[0]), (outer[-1], inner[1])):         # the end faces are rings around the hole
            for k in range(sides):
                flat.append(len(faces))
                faces.append((rim[k], rim[(k + 1) % sides], hole[(k + 1) % sides], hole[k]))
    else:
        for rim in (outer[0], outer[-1]):
            flat.append(len(faces))
            faces.append(tuple(rim))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.validate()
    work = bmesh.new()
    work.from_mesh(mesh)
    bmesh.ops.recalc_face_normals(work, faces=work.faces)
    work.to_mesh(mesh)
    work.free()
    ends = set(flat)
    for poly in mesh.polygons:
        poly.use_smooth = poly.index not in ends
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    textured = False
    if source is not None and surface != "plain":
        textured = _own_surface(obj, source, target)
    if not textured and target is not None:
        mesh.materials.append(target)
    bpy.context.view_layer.update()
    return {"object": obj.name, "length_m": round(length, 4), "outer_radius_m": [round(min(r for _a, r in stations), 5), round(max(r for _a, r in stations), 5)],
            "bore_m": round(bore, 5), "hollow": bore > 0, "triangles": sum(len(p.vertices) - 2 for p in mesh.polygons),
            **({"surface": "the traced photo's own surface"} if textured else {}),
            "next": "look at its open end: if the real one is hollow and this is not, make it again with bore_m."}


# ---------------------------------------------------------------------------------------------
# the photo on the model, and the model measured against the photo


def _extent(objects: List[bpy.types.Object]) -> Tuple[Vector, Vector]:
    corners = [o.matrix_world @ Vector(c) for o in objects for c in o.bound_box]
    return (Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners))),
            Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners))))


def _subject_box(mask) -> Tuple[int, int, int, int]:
    import numpy as np

    rows, columns = np.where(mask.any(axis=1))[0], np.where(mask.any(axis=0))[0]
    return int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1


@tool(
    "project_reference",
    """Put the REAL SURFACE on the model: lays a photo of the real thing over the objects, lined
    up by the subject's outline, as their colour and fine relief. Texture patterns, markings,
    logos, seams, wear and colour changes come straight from the photo instead of being guessed.
    Use it when the form already matches (match_reference reports the outline overlap), with a
    photo taken square-on against a plain or transparent background: the photo you traced is
    ideal. `side` says which side of the model the photo shows: 'front' is what the front view
    sees. A second photo of the other side can be projected with side 'back'. Faces turned away
    from the photo keep the material's own colour. Each object's own material settings
    (roughness, metal) are kept; the photo replaces the colour.""",
    {
        "image": {"type": "string", "description": "Photo of the real thing, square-on, on a plain or transparent background."},
        "objects": {"type": "array", "items": {"type": "string"}, "description": "The objects that make up the model."},
        "side": {"type": "string", "enum": ["front", "back"], "description": "Which side of the model the photo shows (default front)."},
        "mirror": {"type": "boolean", "description": "Flip the photo left to right, when it shows the thing pointing the other way."},
        "bump": {"type": "number", "description": "How much relief to take from the photo's light and dark, 0-1 (default 0.15)."},
    },
    ["image", "objects"],
    mutates=True,
    timeout=180.0,
    label="Projected the photo onto the model",
)
def project_reference(image: str, objects: List[str], side: str = "front", mirror: bool = False,
                      bump: float = 0.15) -> Dict[str, Any]:
    path = bpy.path.abspath(image)
    if not os.path.isfile(path) or os.path.splitext(path)[1].lower() not in _IMAGE_EXTS:
        raise ToolError(f"Not an image file: {image}")
    if side not in ("front", "back"):
        raise ToolError("side is 'front' or 'back'.")
    targets = [bpy.data.objects.get(n) for n in objects]
    missing = [n for n, o in zip(objects, targets) if o is None or o.type != 'MESH']
    if missing or not targets:
        raise ToolError(f"Not mesh objects in this scene: {', '.join(missing) or '(none given)'}")
    import numpy as np

    pixels = _pixels(path)
    mask = _subject(pixels)
    rows, columns = mask.shape
    c0, r0, c1, r1 = _subject_box(mask)
    u0, u1, v0, v1 = c0 / columns, c1 / columns, r0 / rows, r1 / rows
    low, high = _extent(targets)
    width, height = max(high.x - low.x, 1e-9), max(high.z - low.z, 1e-9)
    photo_aspect, model_aspect = (c1 - c0) / max(r1 - r0, 1), width / height
    flip = (side == "back") != bool(mirror)          # seen from behind, left and right swap
    # the photo the model was traced from is laid exactly where it was read, whatever the model's bounds are now
    traced = next((o for o in bpy.data.objects if "unix_photo_frame" in o
                   and os.path.normcase(str(o.get("unix_photo", ""))) == os.path.normcase(path)), None)
    exact = traced is not None and not flip

    _turn.mark_mutation()
    picture = bpy.data.images.load(_padded_photo(path, mask), check_existing=True)
    picture.reload()
    layer_name = f"UnixPhoto_{side}"
    copies: Dict[str, bpy.types.Material] = {}
    tones: Dict[str, Tuple[float, float, float, float]] = {}
    for obj in targets:                 # the photo's own colour where this object is: its rim and far faces take it
        box_low, box_high = _extent([obj])
        across = sorted((((box_low.x - low.x) / width), ((box_high.x - low.x) / width)))
        if flip:
            across = [1.0 - across[1], 1.0 - across[0]]
        down = [(high.z - box_high.z) / height, (high.z - box_low.z) / height]
        column0, column1 = int(c0 + across[0] * (c1 - c0)), int(math.ceil(c0 + across[1] * (c1 - c0)))
        row0, row1 = int(r0 + down[0] * (r1 - r0)), int(math.ceil(r0 + down[1] * (r1 - r0)))
        window = mask[max(row0, 0):row1, max(column0, 0):column1]
        if window.sum() >= 4:
            seen = pixels[max(row0, 0):row1, max(column0, 0):column1][window][:, :3].mean(axis=0)
            linear = np.where(seen <= 0.04045, seen / 12.92, ((seen + 0.055) / 1.055) ** 2.4)
            tones[obj.name] = (float(linear[0]), float(linear[1]), float(linear[2]), 1.0)
    for obj in targets:
        mesh = obj.data
        layer = mesh.uv_layers.get(layer_name) or mesh.uv_layers.new(name=layer_name)
        world = obj.matrix_world
        placed = [world @ v.co for v in mesh.vertices]
        for loop in mesh.loops:
            at = placed[loop.vertex_index]
            if exact:
                frame_left, frame_top, frame_width, frame_height, was_mirrored = traced["unix_photo_frame"]
                across = (at.x - frame_left) / frame_width
                layer.data[loop.index].uv = (1.0 - across if was_mirrored else across, 1.0 - (frame_top - at.z) / frame_height)
                continue
            across = (at.x - low.x) / width
            if flip:
                across = 1.0 - across
            down = (high.z - at.z) / height
            layer.data[loop.index].uv = (u0 + across * (u1 - u0), 1.0 - (v0 + down * (v1 - v0)))
        mesh.uv_layers.active = layer
        layer.active_render = True
        if not mesh.materials:
            mesh.materials.append(bpy.data.materials.new(obj.name + "_Surface"))
        for index, material in enumerate(mesh.materials):
            if material is None:
                material = bpy.data.materials.new(obj.name + "_Surface")
            if material.get("unix_photo_base"):          # already a photo copy: add this side to it
                copy = material
            else:
                copy = copies.get(material.name)
                if copy is None:
                    copy = copies[material.name] = material.copy()
                    copy.name = material.name + "_Photo"
                    copy["unix_photo_base"] = material.name
            mesh.materials[index] = copy
            _photo_layer(copy, picture, layer_name, side, float(bump), tones.get(obj.name))
    bpy.context.view_layer.update()
    out: Dict[str, Any] = {
        "objects": [o.name for o in targets], "side": side, "uv_layer": layer_name,
        "materials": sorted({m.name for o in targets for m in o.data.materials if m is not None}),
        "next": "look at it with shading 'rendered' from the front and at a three-quarter angle. The photo carries its own "
                "light and shadow: keep the scene lighting soft. The other side keeps the plain material until a photo of that "
                "side is projected with side='back'."}
    if not exact and abs(photo_aspect / model_aspect - 1.0) > 0.06:
        out["warning"] = (f"The subject in the photo is {photo_aspect:.2f} times as wide as it is tall, the model {model_aspect:.2f}: "
                          f"the photo was stretched to fit, so features will be off. Fix the model's proportions (or pass every "
                          f"object of the model) and project again.")
    return out


def _photo_layer(material: bpy.types.Material, picture: bpy.types.Image, layer_name: str, side: str, bump: float,
                 tone: Optional[Tuple[float, float, float, float]] = None) -> None:
    """Mix a photo over a material's colour on the faces that look toward the photo's viewpoint.
    ``tone`` is the photo's average colour on this object: faces turned away from the photo get
    it, so the rim, the top and the ends match the photographed side instead of standing out."""
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    shader = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if shader is None:
        shader = nodes.new("ShaderNodeBsdfPrincipled")
        output = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None) or nodes.new("ShaderNodeOutputMaterial")
        links.new(shader.outputs["BSDF"], output.inputs["Surface"])
    tag = f"unix_photo_{side}"
    earlier = nodes.get(tag + "_mix")
    if earlier is not None and nodes.get(tag + "_image") is not None and nodes.get(tag + "_uv") is not None:
        # projected again: the layer stays where it is in the stack, with what lies under and over it; only the picture changes
        nodes[tag + "_image"].image = picture
        nodes[tag + "_uv"].uv_map = layer_name
        if nodes.get(tag + "_bump") is not None:
            nodes[tag + "_bump"].inputs["Strength"].default_value = max(0.0, min(bump, 1.0))
        if tone is not None and not earlier.inputs["A"].is_linked:
            earlier.inputs["A"].default_value = tone
        return
    base = shader.inputs["Base Color"]
    coords = nodes.new("ShaderNodeUVMap")
    coords.name, coords.uv_map = tag + "_uv", layer_name
    image = nodes.new("ShaderNodeTexImage")
    image.name, image.image, image.extension = tag + "_image", picture, 'EXTEND'
    links.new(coords.outputs["UV"], image.inputs["Vector"])
    geometry = nodes.new("ShaderNodeNewGeometry")
    geometry.name = tag + "_geometry"
    split = nodes.new("ShaderNodeSeparateXYZ")
    split.name = tag + "_split"
    links.new(geometry.outputs["Normal"], split.inputs["Vector"])
    facing = nodes.new("ShaderNodeMapRange")                      # 0 where a face looks sideways, 1 where it looks at the camera
    facing.name, facing.clamp = tag + "_facing", True
    low, high = (-0.25, -0.6) if side == "front" else (0.25, 0.6)   # the front view looks along +Y, so its faces point to -Y
    facing.inputs["From Min"].default_value, facing.inputs["From Max"].default_value = low, high
    if side == "both":                                             # the same picture on the near and the far side
        either = nodes.new("ShaderNodeMath")
        either.name, either.operation = tag + "_abs", 'ABSOLUTE'
        links.new(split.outputs["Y"], either.inputs[0])
        links.new(either.outputs["Value"], facing.inputs["Value"])
    else:
        links.new(split.outputs["Y"], facing.inputs["Value"])
    mix = nodes.new("ShaderNodeMix")
    mix.name, mix.data_type, mix.blend_type = tag + "_mix", 'RGBA', 'MIX'
    if base.is_linked:
        links.new(base.links[0].from_socket, mix.inputs["A"])
    else:
        mix.inputs["A"].default_value = tone or base.default_value
    links.new(image.outputs["Color"], mix.inputs["B"])
    links.new(facing.outputs["Result"], mix.inputs["Factor"])
    links.new(mix.outputs["Result"], base)
    if bump > 0:
        relief = nodes.new("ShaderNodeBump")
        relief.name = tag + "_bump"
        relief.inputs["Strength"].default_value = max(0.0, min(bump, 1.0))
        normal = shader.inputs["Normal"]
        if normal.is_linked:
            links.new(normal.links[0].from_socket, relief.inputs["Normal"])
        links.new(image.outputs["Color"], relief.inputs["Height"])
        links.new(relief.outputs["Normal"], normal)


def outline_numbers(path: str, targets: List[bpy.types.Object], cells: int = 220) -> Optional[Dict[str, Any]]:
    """How well the model's outline in the front view matches the subject in a photo: overlap,
    what is missing, what sticks out, and whether the openings are as big. None when the photo's
    background is not plain enough to read the subject from."""
    import numpy as np
    from mathutils.bvhtree import BVHTree

    try:
        photo = _subject(_pixels(path))
    except ToolError:
        return None
    meshes = [o for o in targets if o.type == 'MESH']
    if not meshes:
        return None
    depsgraph = bpy.context.evaluated_depsgraph_get()
    verts: List[Vector] = []
    faces: List[Tuple[int, ...]] = []
    for obj in meshes:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            if len(mesh.polygons) > 600_000:
                continue
            start, world = len(verts), evaluated.matrix_world
            verts += [world @ v.co for v in mesh.vertices]
            faces += [tuple(start + i for i in poly.vertices) for poly in mesh.polygons]
        finally:
            evaluated.to_mesh_clear()
    if not faces:
        return None
    tree = BVHTree.FromPolygons(verts, faces)
    low = Vector((min(v.x for v in verts), min(v.y for v in verts), min(v.z for v in verts)))
    high = Vector((max(v.x for v in verts), max(v.y for v in verts), max(v.z for v in verts)))
    width, height = max(high.x - low.x, 1e-9), max(high.z - low.z, 1e-9)
    columns = cells if width >= height else max(8, int(round(cells * width / height)))
    rows = cells if height > width else max(8, int(round(cells * height / width)))
    model = np.zeros((rows, columns), dtype=bool)
    along = Vector((0.0, 1.0, 0.0))
    for row in range(rows):
        z = high.z - (row + 0.5) * height / rows
        for column in range(columns):
            origin = Vector((low.x + (column + 0.5) * width / columns, low.y - 1.0, z))
            model[row, column] = tree.ray_cast(origin, along)[0] is not None
    c0, r0, c1, r1 = _subject_box(photo)
    pick_rows = (r0 + (np.arange(rows) + 0.5) * (r1 - r0) / rows).astype(int)
    pick_columns = (c0 + (np.arange(columns) + 0.5) * (c1 - c0) / columns).astype(int)
    real = photo[np.ix_(pick_rows, pick_columns)]

    def openings(mask) -> float:
        border = [(0, c) for c in range(columns)] + [(rows - 1, c) for c in range(columns)] + \
                 [(r, 0) for r in range(rows)] + [(r, columns - 1) for r in range(rows)]
        return float((~mask & ~_region(~mask, border)).sum())

    def score(reference) -> float:
        return float((reference & model).sum()) / max(float((reference | model).sum()), 1.0)

    straight, flipped = score(real), score(real[:, ::-1])
    out: Dict[str, Any] = {}
    if flipped > straight + 0.08:
        real = real[:, ::-1]
        out["mirrored"] = ("The model points the opposite way to the photo. The numbers below compare it with the photo flipped; "
                           "pass mirror=true when you trace or project this photo.")
    area = max(float(real.sum()), 1.0)
    missing, extra = real & ~model, model & ~real
    out["outline_overlap_percent"] = round(100 * max(straight, flipped))
    out["real_outline_not_covered_percent"] = round(100 * float(missing.sum()) / area, 1)
    out["model_outside_real_outline_percent"] = round(100 * float(extra.sum()) / area, 1)
    for label, cells_off in (("not_covered_mostly", missing), ("outside_mostly", extra)):
        if cells_off.sum() > 0.02 * area:
            at_rows, at_columns = np.nonzero(cells_off)
            vertical = ("top", "middle", "bottom")[min(2, int(3 * at_rows.mean() / rows))]
            horizontal = ("left", "centre", "right")[min(2, int(3 * at_columns.mean() / columns))]
            out[label] = f"{vertical} {horizontal}"
    real_open, model_open = openings(real), openings(model)
    out["openings_percent_of_area"] = {"photo": round(100 * real_open / area, 1), "model": round(100 * model_open / area, 1)}
    verdict = []
    if real_open > 0.004 * area and model_open < 0.7 * real_open:
        verdict.append(f"the model's openings are {round(100 * model_open / real_open)}% of the size they have in the photo: "
                       f"something is filling them (a part pushed in, a rounding that closed them). Open them up.")
    if out["real_outline_not_covered_percent"] > 3:
        verdict.append(f"{out['real_outline_not_covered_percent']}% of the real outline is missing from the model"
                       + (f" ({out['not_covered_mostly']})" if "not_covered_mostly" in out else "") + ".")
    if out["model_outside_real_outline_percent"] > 3:
        verdict.append(f"{out['model_outside_real_outline_percent']}% of the model lies outside the real outline"
                       + (f" ({out['outside_mostly']})" if "outside_mostly" in out else "") + ".")
    out["verdict"] = " ".join(verdict) or "The outline and its openings match the photo."
    return out
