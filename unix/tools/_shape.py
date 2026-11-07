# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Shape analysis shared by the rigging tools. No tools are registered here.

Three things a renderer cannot tell a model, and guessing gets wrong on small parts:

* **Seams.** Imported meshes are often split into pieces whose border vertices only touch
  (one vertex per piece at the same position). ``Welded`` treats touching vertices as one.
* **Limbs.** ``analyse`` measures surface distance from a root and watches the far side of
  the surface as the distance drops: every tip starts a region, and where two long regions
  meet a limb ends. Cutting each limb at even distances gives rings whose centres form a
  centre line through the middle of each finger, leg or tail.
* **Inside.** ``Volume`` answers "is this point inside the mesh" and "where is the middle
  of the mesh around this point".
"""

from __future__ import annotations

import heapq
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree

FINGERS = ("thumb", "index", "middle", "ring", "pinky")


class Welded:
    """A mesh's vertices in world space with touching vertices merged into nodes."""

    def __init__(self, obj: bpy.types.Object) -> None:
        mesh = obj.data
        world = obj.matrix_world
        self.co: List[Vector] = [world @ v.co for v in mesh.vertices]
        count = len(self.co)
        lo = Vector((min((p.x for p in self.co), default=0.0), min((p.y for p in self.co), default=0.0),
                     min((p.z for p in self.co), default=0.0)))
        hi = Vector((max((p.x for p in self.co), default=0.0), max((p.y for p in self.co), default=0.0),
                     max((p.z for p in self.co), default=0.0)))
        self.lo, self.hi = lo, hi
        self.size = max((hi - lo).length, 1e-9)
        self.eps = max(self.size * 1e-5, 1e-7)
        tree = KDTree(count)
        for index, point in enumerate(self.co):
            tree.insert(point, index)
        tree.balance()
        self.kd = tree
        rep = list(range(count))
        for index, point in enumerate(self.co):
            if rep[index] != index:
                continue
            for _co, other, _dist in tree.find_range(point, self.eps):
                if other > index and rep[other] == other:
                    rep[other] = index
        self.rep = rep
        order: Dict[int, int] = {}
        for index in range(count):
            if rep[index] == index:
                order[index] = len(order)
        self.node_of: List[int] = [order[rep[index]] for index in range(count)]   # vertex -> node
        self.pos: List[Vector] = [self.co[index] for index in order]              # node -> position
        self.members: List[List[int]] = [[] for _ in order]                       # node -> vertices
        for index, node in enumerate(self.node_of):
            self.members[node].append(index)
        adjacency: List[Dict[int, float]] = [{} for _ in order]
        for edge in mesh.edges:
            a, b = self.node_of[edge.vertices[0]], self.node_of[edge.vertices[1]]
            if a != b and b not in adjacency[a]:
                length = (self.pos[a] - self.pos[b]).length
                adjacency[a][b] = length
                adjacency[b][a] = length
        self.adj = adjacency
        mesh.calc_loop_triangles()
        self.tris: List[Tuple[int, int, int]] = []
        for tri in mesh.loop_triangles:
            a, b, c = (self.node_of[v] for v in tri.vertices)
            if a != b and b != c and a != c:
                self.tris.append((a, b, c))
        self._components: Optional[List[List[int]]] = None

    def twin_groups(self) -> List[List[int]]:
        """Groups of vertex indices that share one position (seam vertices)."""
        return [group for group in self.members if len(group) > 1]

    def components(self) -> List[List[int]]:
        """Connected pieces as lists of nodes, biggest first."""
        if self._components is None:
            seen = [False] * len(self.pos)
            pieces = []
            for start in range(len(self.pos)):
                if seen[start]:
                    continue
                seen[start] = True
                stack, piece = [start], []
                while stack:
                    node = stack.pop()
                    piece.append(node)
                    for other in self.adj[node]:
                        if not seen[other]:
                            seen[other] = True
                            stack.append(other)
                pieces.append(piece)
            pieces.sort(key=len, reverse=True)
            self._components = pieces
        return self._components

    def diagonal(self, nodes: Sequence[int]) -> float:
        xs = [self.pos[n] for n in nodes]
        return (Vector((max(p.x for p in xs), max(p.y for p in xs), max(p.z for p in xs)))
                - Vector((min(p.x for p in xs), min(p.y for p in xs), min(p.z for p in xs)))).length

    def riders(self) -> List[Tuple[List[int], int]]:
        """Small loose pieces that sit on a big one (fingernails, buttons, eyes): each as
        (its vertex indices, the vertex of a big piece nearest to its centre)."""
        pieces = self.components()
        if len(pieces) < 2:
            return []
        biggest = len(pieces[0])
        reach = self.diagonal(pieces[0])
        big = [p for p in pieces if len(p) >= biggest * 0.1]
        small = [p for p in pieces if len(p) < biggest * 0.1]
        if not small:
            return []
        anchors = [n for piece in big for n in piece]
        tree = KDTree(len(anchors))
        for slot, node in enumerate(anchors):
            tree.insert(self.pos[node], slot)
        tree.balance()
        out = []
        for piece in small:
            span = self.diagonal(piece) if len(piece) > 1 else 0.0
            if span > reach * 0.2:
                continue
            centre = sum((self.pos[n] for n in piece), Vector()) / len(piece)
            _co, slot, dist = tree.find(centre)
            if dist <= max(span, self.size * 0.002) * 1.5:
                out.append(([v for n in piece for v in self.members[n]], self.members[anchors[slot]][0]))
        return out

    def distances(self, source, allowed: Optional[set] = None) -> Dict[int, float]:
        """Surface distance from one node (or the nearest of several) to every node it can reach."""
        sources = [source] if isinstance(source, int) else list(source)
        dist = {node: 0.0 for node in sources}
        heap = [(0.0, node) for node in sources]
        while heap:
            d, node = heapq.heappop(heap)
            if d > dist.get(node, math.inf):
                continue
            for other, length in self.adj[node].items():
                if allowed is not None and other not in allowed:
                    continue
                nd = d + length
                if nd < dist.get(other, math.inf):
                    dist[other] = nd
                    heapq.heappush(heap, (nd, other))
        return dist


_RAYS = [Vector(d).normalized() for d in ((1, 0.137, 0.071), (-1, 0.093, -0.127), (0.113, 1, 0.059),
                                           (-0.081, -1, 0.143), (0.067, -0.121, 1), (0.149, 0.083, -1), (0.577, 0.611, 0.541))]


class Volume:
    """Inside/outside and centre-of-volume queries against a mesh in world space."""

    def __init__(self, obj: bpy.types.Object) -> None:
        world = obj.matrix_world
        verts = [world @ v.co for v in obj.data.vertices]
        polys = [tuple(p.vertices) for p in obj.data.polygons]
        self.tree = BVHTree.FromPolygons(verts, polys) if verts and polys else None

    def inside(self, point: Vector) -> Tuple[bool, float]:
        """(is the point inside the surface, its distance to the surface).

        Follows rays from the point and counts the walls they leave through against the
        walls they enter through: from inside, a ray leaves once more than it enters. The
        majority of several directions decides, so open ends do not fool it, and a point
        inside two overlapping shells (a wing pushed into a body) still counts as inside."""
        if self.tree is None:
            return False, math.inf
        location, _normal, _index, distance = self.tree.find_nearest(point)
        if location is None:
            return False, math.inf
        votes = 0
        for direction in _RAYS:
            origin, balance = point, 0
            for _hit in range(64):
                hit = self.tree.ray_cast(origin, direction)
                if hit[0] is None:
                    break
                balance += 1 if hit[1].dot(direction) > 0.0 else -1     # leaving : entering
                origin = hit[0] + direction * max(1e-6, hit[3] * 1e-6)
            votes += 1 if balance > 0 else 0
        return votes * 2 > len(_RAYS), distance

    def depth(self, point: Vector) -> Tuple[float, float]:
        """(distance to the nearest wall, distance to the wall on the opposite side); (0, 0) outside."""
        inside, near = self.inside(point)
        if not inside or near < 1e-9:
            return 0.0, 0.0
        location = self.tree.find_nearest(point)[0]
        if location is None or (point - location).length < 1e-12:
            return 0.0, 0.0
        away = (point - location).normalized()
        hit = self.tree.ray_cast(point, away)
        return near, (hit[3] if hit[0] is not None else near)

    def centre(self, point: Vector, axis: Optional[Vector] = None, rounds: int = 4) -> Vector:
        """Move a point to the middle of the mesh around it: halfway between the nearest wall and
        the wall opposite it, repeated. In a tube that is the axis; in a thin sheet (a wing
        membrane, an ear, a fin) it is the middle of the thickness, without sliding along the
        sheet. ``axis`` (the way the limb runs) keeps the point from sliding along the limb."""
        if self.tree is None:
            return point
        unit = axis.normalized() if axis is not None and axis.length > 1e-9 else None
        current = point.copy()
        for _round in range(rounds):
            location = self.tree.find_nearest(current)[0]
            if location is None:
                break
            inside, near = self.inside(current)
            if near < 1e-9:
                break
            if inside:
                way = (current - location).normalized()
                far = self.tree.ray_cast(current, way)
            else:               # step through the wall first
                way = (location - current).normalized()
                far = self.tree.ray_cast(location + way * max(1e-6, near * 1e-4), way)
            if far[0] is None:
                break
            shift = location.lerp(far[0], 0.5) - current
            if unit is not None:
                shift -= unit * shift.dot(unit)
            current = current + shift
            if shift.length < near * 0.02:
                break
        return current


# ---------------------------------------------------------------------------------------------
# limbs


def _median(values: List[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else 0.0


def _ring_radius(points: List[Vector]) -> float:
    centre = sum(points, Vector()) / len(points)
    return sum((p - centre).length for p in points) / len(points)


def _polyline_length(points: List[Vector]) -> float:
    return sum((points[i + 1] - points[i]).length for i in range(len(points) - 1))


def point_at(points: List[Vector], t: float) -> Vector:
    """The point a fraction t (0..1) of the way along a polyline, by length."""
    if len(points) == 1:
        return points[0].copy()
    total = _polyline_length(points)
    target = max(0.0, min(1.0, t)) * total
    walked = 0.0
    for i in range(len(points) - 1):
        step = (points[i + 1] - points[i]).length
        if walked + step >= target and step > 1e-12:
            return points[i].lerp(points[i + 1], (target - walked) / step)
        walked += step
    return points[-1].copy()


def _value_at(points: List[Vector], values: List[float], t: float) -> float:
    total = _polyline_length(points)
    target = max(0.0, min(1.0, t)) * total
    walked = 0.0
    for i in range(len(points) - 1):
        step = (points[i + 1] - points[i]).length
        if walked + step >= target and step > 1e-12:
            f = (target - walked) / step
            return values[i] * (1 - f) + values[min(i + 1, len(values) - 1)] * f
        walked += step
    return values[-1]


def _pick_root(welded: Welded, piece: List[int], near: Optional[Vector]) -> List[int]:
    """Nodes to measure from. A named point gives one node; otherwise the whole thick end of
    the piece, so the very first cuts are already rings around the limb."""
    allowed = set(piece)
    if near is not None:
        return [min(piece, key=lambda n: (welded.pos[n] - near).length_squared)]
    first = welded.distances(piece[0], allowed)
    end_a = max(first, key=first.get)
    from_a = welded.distances(end_a, allowed)
    end_b = max(from_a, key=from_a.get)
    from_b = welded.distances(end_b, allowed)
    span = from_a[end_b]

    def thickness(dist: Dict[int, float]) -> float:
        ring = [welded.pos[n] for n, d in dist.items() if span * 0.06 <= d <= span * 0.14]
        return _ring_radius(ring) if len(ring) >= 3 else 0.0

    # A limb is attached at its thick end and tapers to its tips.
    other = from_b if thickness(from_a) >= thickness(from_b) else from_a    # measured from the thin end
    furthest = max(other.values())
    return [n for n, d in other.items() if d >= furthest - span * 0.05]


def _analyse_piece(welded: Welded, piece: List[int], sources: List[int], detail: int, smallest: float,
                   volume: "Volume") -> Dict[str, Any]:
    allowed = set(piece)
    root = sources[0]
    dist = welded.distances(sources, allowed)
    span = max(dist.values())
    minimum = max(smallest * span, 1e-9)
    width = max(span / max(8, detail), 1e-9)

    # Sweep from the furthest point back to the root. Each tip starts a region; when regions
    # meet, short ones are bumps and are absorbed, long ones are limbs and end there.
    link: Dict[int, int] = {}
    info: Dict[int, Dict[str, Any]] = {}
    branches: List[Dict[str, Any]] = []

    def find(node: int) -> int:
        while link[node] != node:
            link[node] = link[link[node]]
            node = link[node]
        return node

    def open_branch(top: float, tip: int, children: List[int]) -> int:
        branches.append({"nodes": [], "top": top, "tip_node": tip, "low": 0.0, "parent": -1,
                         "children": children, "dead": False})
        return len(branches) - 1

    for node in sorted(piece, key=lambda n: dist[n], reverse=True):
        here = dist[node]
        around = list({find(other) for other in welded.adj[node] if other in link})
        link[node] = node
        if not around:
            branch = open_branch(here, node, [])
            branches[branch]["nodes"].append(node)
            info[node] = {"top": here, "branch": branch, "tip": node}
            continue
        limbs = [c for c in around if info[c]["top"] - here >= minimum
                 and (branches[info[c]["branch"]]["children"] or len(branches[info[c]["branch"]]["nodes"]) >= 8)]
        best = max(around, key=lambda c: info[c]["top"])
        if len(limbs) >= 2:
            target = open_branch(info[best]["top"], info[best]["tip"], [info[c]["branch"] for c in limbs])
            for c in limbs:
                branches[info[c]["branch"]].update(low=here, parent=target)
        elif len(limbs) == 1:
            target = info[limbs[0]]["branch"]
        else:
            target = info[best]["branch"]
        for c in around:        # bumps join the branch they sit on
            branch = info[c]["branch"]
            if branch != target and c not in limbs:
                branches[target]["nodes"].extend(branches[branch]["nodes"])
                branches[branch].update(nodes=[], dead=True)
        branches[target]["nodes"].append(node)
        top, tip = info[best]["top"], info[best]["tip"]
        for c in around:
            link[c] = node
            del info[c]
        info[node] = {"top": top, "branch": target, "tip": tip}

    trunk = info[find(root)]["branch"]
    label: Dict[int, int] = {}
    for index, branch in enumerate(branches):
        for node in branch["nodes"]:
            label[node] = index

    # Rings: cut every branch at even surface distances and measure each cut's centre and radius.
    cuts: Dict[Tuple[int, int], List[Tuple[Vector, float]]] = {}
    offset = 0.25 * width
    pos = welded.pos
    for tri in welded.tris:
        if tri[0] not in allowed:
            continue
        a, b, c = sorted(tri, key=lambda n: dist[n])
        d_lo, d_mid, d_hi = dist[a], dist[b], dist[c]
        if d_hi - d_lo < 1e-12:
            continue
        branch = label[c]
        upper = d_hi
        while branch >= 0 and upper > d_lo:
            low = branches[branch]["low"]
            lower = max(d_lo, low)
            j = max(0, math.ceil((lower - low - offset) / width + 1e-9))
            while True:
                level = low + offset + j * width
                if level > upper:
                    break
                if level > lower:
                    first = pos[a].lerp(pos[c], (level - d_lo) / (d_hi - d_lo))
                    if level <= d_mid and d_mid - d_lo > 1e-12:
                        second = pos[a].lerp(pos[b], (level - d_lo) / (d_mid - d_lo))
                    elif d_hi - d_mid > 1e-12:
                        second = pos[b].lerp(pos[c], (level - d_mid) / (d_hi - d_mid))
                    else:
                        second = pos[b]
                    length = (second - first).length
                    if length > 1e-12:
                        cuts.setdefault((branch, j), []).append(((first + second) / 2, length))
                j += 1
            if lower <= d_lo:
                break
            upper = lower
            branch = branches[branch]["parent"]

    segments: List[Dict[str, Any]] = []
    remap: Dict[int, int] = {}
    for index in [trunk] + [i for i in range(len(branches)) if i != trunk and not branches[i]["dead"]]:
        branch = branches[index]
        leaf = not branch["children"]
        centres, radii = [], []
        for j in sorted(j for (b, j) in cuts if b == index):
            pieces = cuts[(index, j)]
            total = sum(length for _mid, length in pieces)
            centre = sum((mid * length for mid, length in pieces), Vector()) / total
            radius = sum((mid - centre).length * length for mid, length in pieces) / total
            level = offset + j * width
            # measured from a single point, the first cuts are patches on one side of the body, not rings
            if index == trunk and len(sources) == 1 and radius > 0.42 * level and len(branch["nodes"]) > 8:
                continue
            centres.append(centre)
            radii.append(radius)
        tip = pos[branch["tip_node"]].copy()
        if index == trunk and centres:
            start = sum((pos[n] for n in sources), Vector()) / len(sources)
            spread = sum((pos[n] - start).length for n in sources) / len(sources)
            if len(sources) == 1:      # a surface point: move it to the middle of the body
                start = volume.centre(start, centres[0] - start)
                spread = radii[0]
            if (centres[0] - start).length > width * 0.5:
                centres.insert(0, start)
                radii.insert(0, spread)
        if not centres:
            centres, radii = [sum((pos[n] for n in branch["nodes"]), Vector()) / len(branch["nodes"])], [0.0]
        line = list(centres)
        if leaf:
            while len(line) > 1 and (line[-1] - tip).length < width * 0.5:
                line.pop()
            line.append(tip)
        for _pass in range(1 if len(line) > 2 else 0):   # light smoothing, ends fixed
            line = [line[0]] + [(line[i - 1] + line[i] * 2 + line[i + 1]) / 4 for i in range(1, len(line) - 1)] + [line[-1]]
        remap[index] = len(segments)
        segments.append({"parent": branch["parent"], "children": branch["children"], "leaf": leaf,
                         "start_distance": branch["low"], "tip_distance": branch["top"], "tip": tip,
                         "line": line, "centres": centres, "radii": radii, "length": _polyline_length(line),
                         "nodes": len(branch["nodes"])})
    for seg in segments:
        seg["parent"] = remap.get(seg["parent"], -1)
        seg["children"] = [remap[c] for c in seg["children"] if c in remap]
    return {"segments": segments, "span": span, "band_width": width, "root": segments[0]["line"][0].copy()}


def narrow_points(seg: Dict[str, Any]) -> List[Tuple[float, Vector, float]]:
    """Where a limb is thinnest between thicker stretches (wrists, ankles, necks): (t, point, radius)."""
    centres, radii = seg["centres"], seg["radii"]
    count = len(radii)
    if count < 7:
        return []
    smooth = [sum(radii[max(0, i - 1):i + 2]) / len(radii[max(0, i - 1):i + 2]) for i in range(count)]
    total = _polyline_length(centres)
    walked = [0.0]
    for i in range(count - 1):
        walked.append(walked[-1] + (centres[i + 1] - centres[i]).length)
    window = max(2, count // 12)
    out: List[Tuple[int, float]] = []
    for i in range(window, count - window):
        around = smooth[i - window:i + window + 1]
        if smooth[i] == min(around) and max(around) > smooth[i] * 1.06 and not (out and i - out[-1][0] <= window):
            out.append((i, smooth[i]))
    return [(walked[i] / total if total else 0.0, centres[i].copy(), radius) for i, radius in out]


def wrist_of(trunk: Dict[str, Any]) -> Vector:
    """The narrowest point of the last stretch of an arm, before it widens into the hand."""
    late = [p for p in narrow_points(trunk) if p[0] > 0.6]
    return late[-1][1] if late else point_at(trunk["centres"], 0.87)


def _converge(lines: List[Tuple[Vector, Vector]]) -> Optional[Vector]:
    """The point closest to several lines (each a point and a direction): where they fan out from."""
    rows = [[0.0, 0.0, 0.0] for _ in range(3)]
    rhs = [0.0, 0.0, 0.0]
    for point, direction in lines:
        for i in range(3):
            for j in range(3):
                m = (1.0 if i == j else 0.0) - direction[i] * direction[j]
                rows[i][j] += m
                rhs[i] += m * point[j]
    try:
        return Matrix(rows).inverted() @ Vector(rhs)
    except ValueError:      # the lines are parallel
        return None


def _fan(limbs: List[Dict[str, Any]], stem: Dict[str, Any], volume: Volume) -> Optional[Dict[str, Any]]:
    """Limbs that spread from one place: fingers, toes, claws, wing spars, fin rays.
    Orders them across the fan, finds the plane they lie in and where they spread from.
    Five of them are read as a hand (a foot looks the same) and named."""
    middle = {id(s): point_at(s["line"], 0.5) for s in limbs}
    thumb = None
    rest = list(limbs)
    if len(limbs) == 5:
        thumb = min(limbs, key=lambda s: s["start_distance"])
        rest = [s for s in limbs if s is not thumb]
    first, last = max(((a, b) for a in rest for b in rest), key=lambda pair: (middle[id(pair[0])] - middle[id(pair[1])]).length)
    if thumb is not None and (middle[id(last)] - thumb["line"][0]).length < (middle[id(first)] - thumb["line"][0]).length:
        first, last = last, first      # the index finger is the end of the row nearest the thumb
    row = middle[id(last)] - middle[id(first)]
    rest.sort(key=lambda s: (middle[id(s)] - middle[id(first)]).dot(row))
    along = sum(((s["line"][-1] - s["line"][0]).normalized() for s in rest), Vector())
    if along.length < 1e-6:
        return None
    along.normalize()
    across = row - along * row.dot(along)
    if across.length < 1e-9:
        return None
    normal = along.cross(across.normalized()).normalized()
    mean_length = sum(s["length"] for s in rest) / len(rest)
    sure = False
    if thumb is not None:
        for seg, name in zip([thumb] + rest, FINGERS):
            seg["guess"] = name
        # relaxed fingers curl toward the palm: their middles bulge to the back of the hand
        bulge = sum((middle[id(s)] - (s["line"][0] + s["line"][-1]) / 2).dot(normal) for s in rest)
        centre = sum((p for s in rest for p in (s["line"][0], s["line"][-1])), Vector()) / (2 * len(rest))
        lean = (thumb["line"][-1] - centre).dot(normal)      # the thumb tip sits on the palm side
        if abs(bulge) > mean_length * 0.04:
            normal, sure = (-normal if bulge > 0 else normal), True
        elif abs(lean) > mean_length * 0.1:
            normal, sure = (normal if lean > 0 else -normal), True
        origin = wrist_of(stem)
    else:
        bases = sum((s["line"][0] for s in rest), Vector()) / len(rest)
        origin = _converge([(s["line"][0], (s["line"][-1] - s["line"][0]).normalized()) for s in rest])
        reach = 1.5 * max(s["length"] for s in rest)
        if origin is None or (origin - bases).dot(along) > 0:        # parallel, or "converging" out in front
            origin = bases - along * mean_length * 0.5
        elif (origin - bases).length > reach:
            origin = bases + (origin - bases).normalized() * reach
        for _try in range(12):                                         # keep it inside the body
            if volume.inside(origin)[0]:
                break
            origin = origin.lerp(bases, 0.2)
        origin = volume.centre(origin)
    ordered = ([thumb] if thumb is not None else []) + rest
    return {"limbs": ordered, "hand": thumb is not None, "normal": normal, "side_known": sure, "along": along,
            "origin": origin, "stem": stem}


def _leaves_under(segments: List[Dict[str, Any]], index: int) -> List[int]:
    seg = segments[index]
    if seg["leaf"]:
        return [index]
    return [leaf for child in seg["children"] for leaf in _leaves_under(segments, child)]


def analyse(obj: bpy.types.Object, root: Optional[Vector] = None, detail: int = 120,
            smallest: float = 0.03) -> Dict[str, Any]:
    """Limbs of a mesh. Returns parts (connected bodies). Each part has a ``trunk`` (root to
    the first fork), ``stems`` (long runs between forks: the arms, legs, neck and wing arms of
    a whole body), ``limbs`` (branches that end in a tip) and ``fans`` (three to eight short
    limbs spreading from one stem: a hand, a foot, a wing's spars)."""
    welded = Welded(obj)
    volume = Volume(obj)
    pieces = welded.components()
    if not pieces:
        return {"parts": [], "small_pieces": 0, "size": welded.size}
    biggest = len(pieces[0])
    bodies = [p for p in pieces if len(p) >= max(24, biggest * 0.1)]
    near_piece = None
    if root is not None:
        near_piece = min(range(len(bodies)), key=lambda i: min((welded.pos[n] - root).length_squared for n in bodies[i]))
    parts = []
    for index, piece in enumerate(bodies[:8]):
        start = _pick_root(welded, piece, root if index == near_piece else None)
        result = _analyse_piece(welded, piece, start, detail, smallest, volume)
        segments = result["segments"]
        letter = chr(ord("A") + index)
        trunk = segments[0]
        trunk["id"] = f"{letter}.trunk"
        stems = sorted((s for s in segments[1:] if not s["leaf"] and s["length"] >= smallest * result["span"]),
                       key=lambda s: s["start_distance"])
        for number, seg in enumerate(stems, start=1):
            seg["id"] = f"{letter}.s{number}"

        fans = []
        taken: set = set()
        queue = [0]
        while queue:                       # from the root outward, so the widest fitting group wins
            at = queue.pop(0)
            seg = segments[at]
            if seg["leaf"]:
                continue
            queue.extend(seg["children"])
            leaves = _leaves_under(segments, at)
            if not 3 <= len(leaves) <= 8 or taken & set(leaves):
                continue
            members = [segments[i] for i in leaves]
            # a fan's limbs are short next to what they sit on (fingers on an arm, not legs on a pelvis)
            if sum(m["length"] for m in members) / len(members) > 1.5 * min(m["start_distance"] for m in members):
                continue
            fan = _fan(members, seg, volume)
            if fan is None:
                continue
            taken |= set(leaves)
            for member in members:
                member["fan"] = fan
            fans.append(fan)

        limbs = [m for fan in fans for m in fan["limbs"]]
        limbs += sorted((s for s in segments[1:] if s["leaf"] and "fan" not in s), key=lambda s: s["start_distance"])
        for number, seg in enumerate(limbs, start=1):
            seg["id"] = f"{letter}.{number}"
        parts.append({"part": letter, "nodes": len(piece), "root": result["root"], "span": result["span"],
                      "trunk": trunk, "stems": stems, "limbs": limbs, "fans": fans})
    return {"parts": parts, "small_pieces": len(pieces) - len(bodies), "size": welded.size}
