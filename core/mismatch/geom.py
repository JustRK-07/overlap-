#!/usr/bin/env python3
"""core/mismatch/geom.py — pure 2-D geometry helpers for the mismatch stage.

Everything here is plain **plan view** maths: numpy arrays of ``(x, y)``
pairs, no DXF entities, no policy, no drawing units.  The checks of
PLAN_MISMATCH §1.3 are written against this module so that the fiddly parts
— separating-axis tests, convex clipping, vertex-to-boundary distances —
can be unit-tested without building a drawing.

Conventions
-----------
* Polygons are ``(N, 2)`` float arrays, vertices in boundary order, **not**
  repeating the first vertex.  ``as_points`` accepts anything flat enough
  to reshape, including a list of ``Vec2``.
* "Signed distance to a boundary" is **positive outside**, negative inside,
  matching §1.3 C (``OUTSIDE_OUTLINE`` / ``EDGE_DISTANCE``).
* Every routine is deterministic and free of RNG; ties are broken by
  argument order so repeated runs agree.
"""

from __future__ import annotations

import math

import numpy as np

__all__ = [
    "EPS",
    "as_points",
    "polygon_area",
    "is_convex",
    "convex_hull",
    "point_in_polygon",
    "point_segment_distance",
    "segment_distance",
    "segment_axis_params",
    "rect_corners",
    "is_rectangle",
    "sat_penetration",
    "signed_boundary_distances",
    "convex_clip_area",
    "polygon_overlap_fraction",
    "kabsch_rigid",
    "rigid_apply",
    "unit",
]

#: Below this a length, a cross product or a squared norm counts as zero.
EPS = 1e-12


# ── basics ──────────────────────────────────────────────────────────────────

def as_points(poly) -> np.ndarray:
    """Coerce ``poly`` to a finite ``(N, 2)`` float array (never empty)."""
    a = np.asarray(poly, dtype=float)
    if a.size == 0:
        return np.zeros((0, 2), dtype=float)
    a = a.reshape(-1, a.shape[-1])[:, :2]
    return np.ascontiguousarray(a, dtype=float)


def unit(v) -> tuple[float, float]:
    """Unit vector of ``v``; ``(0.0, 0.0)`` for a degenerate input."""
    dx, dy = float(v[0]), float(v[1])
    n = math.hypot(dx, dy)
    if n < EPS:
        return (0.0, 0.0)
    return (dx / n, dy / n)


def polygon_area(poly) -> float:
    """Signed area of a closed vertex ring (positive = counter-clockwise)."""
    p = as_points(poly)
    if len(p) < 3:
        return 0.0
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


def is_convex(poly) -> bool:
    """True when every turn of the ring has the same sign.

    Collinear triples (turn ≈ 0) are ignored, so a rectangle drawn with a
    redundant mid-vertex is still convex.
    """
    p = as_points(poly)
    n = len(p)
    if n < 3:
        return False
    sign = 0
    scale = max(float(np.abs(p).max()), 1.0)
    tol = 1e-9 * scale * scale
    for i in range(n):
        a, b, c = p[i], p[(i + 1) % n], p[(i + 2) % n]
        cr = ((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]))
        if abs(cr) <= tol:
            continue
        s = 1 if cr > 0 else -1
        if sign and s != sign:
            return False
        sign = s
    return sign != 0


def convex_hull(points) -> np.ndarray:
    """Andrew's monotone-chain hull, counter-clockwise, no collinear points."""
    p = as_points(points)
    if len(p) < 3:
        return p.copy()
    order = sorted(range(len(p)), key=lambda i: (float(p[i, 0]), float(p[i, 1])))
    uniq = []
    for i in order:
        pt = (float(p[i, 0]), float(p[i, 1]))
        if not uniq or uniq[-1] != pt:
            uniq.append(pt)

    def half(seq):
        out: list[tuple[float, float]] = []
        for pt in seq:
            while len(out) >= 2:
                (x1, y1), (x2, y2) = out[-2], out[-1]
                cr = (x2 - x1) * (pt[1] - y1) - (y2 - y1) * (pt[0] - x1)
                if cr <= 0:
                    out.pop()
                else:
                    break
            out.append(pt)
        return out

    lower = half(uniq)
    upper = half(reversed(uniq))
    return np.asarray(lower[:-1] + upper[:-1], dtype=float)


def point_in_polygon(pt, poly) -> bool:
    """Ray-casting containment; a point exactly on the boundary is inside."""
    p = as_points(poly)
    n = len(p)
    if n < 3:
        return False
    x, y = float(pt[0]), float(pt[1])
    inside = False
    for i in range(n):
        ax, ay = float(p[i, 0]), float(p[i, 1])
        bx, by = float(p[(i + 1) % n, 0]), float(p[(i + 1) % n, 1])
        if _on_segment((x, y), (ax, ay), (bx, by)):
            return True
        if (ay > y) != (by > y):
            xin = ax + (y - ay) * (bx - ax) / (by - ay)
            if x < xin:
                inside = not inside
    return inside


def _on_segment(p, a, b) -> bool:
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < EPS:
        return math.hypot(px - ax, py - ay) <= 1e-9
    t = ((px - ax) * dx + (py - ay) * dy) / L2
    if t < -1e-12 or t > 1.0 + 1e-12:
        return False
    return abs((px - ax) * dy - (py - ay) * dx) / math.sqrt(L2) <= 1e-9


def point_segment_distance(p, a, b) -> float:
    """Distance from ``p`` to the closed segment ``a``–``b``."""
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < EPS:
        return math.hypot(float(p[0]) - ax, float(p[1]) - ay)
    t = ((float(p[0]) - ax) * dx + (float(p[1]) - ay) * dy) / L2
    t = max(0.0, min(1.0, t))
    return math.hypot(float(p[0]) - (ax + t * dx), float(p[1]) - (ay + t * dy))


def _segments_cross(p1, p2, p3, p4) -> bool:
    def ccw(a, b, c):
        return ((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) > 0
    return ccw(p1, p3, p4) != ccw(p2, p3, p4) and ccw(p1, p2, p3) != ccw(p1, p2, p4)


def segment_distance(s1, s2) -> float:
    """Minimum distance between two closed segments (0 when they cross)."""
    a, b = (tuple(map(float, s1[0])), tuple(map(float, s1[1])))
    c, d = (tuple(map(float, s2[0])), tuple(map(float, s2[1])))
    if _segments_cross(a, b, c, d):
        return 0.0
    return min(point_segment_distance(a, c, d), point_segment_distance(b, c, d),
               point_segment_distance(c, a, b), point_segment_distance(d, a, b))


# ── rectangles ──────────────────────────────────────────────────────────────

def is_rectangle(pts, *, angle_tol_deg: float = 0.5, rel_tol: float = 1e-6) -> bool:
    """Four distinct points whose interior angles are all 90° ± ``angle_tol_deg``.

    Checked on the points **as given**, which is why ``features.py`` calls
    it only after the world transform: a rectangle drawn inside a block
    that is inserted with a non-uniform scale is not a rectangle any more.
    """
    p = as_points(pts)
    if len(p) != 4:
        return False
    # distinct vertices
    scale = max(float(np.abs(p).max()), 1.0)
    for i in range(4):
        for j in range(i + 1, 4):
            if math.hypot(p[i, 0] - p[j, 0], p[i, 1] - p[j, 1]) <= rel_tol * scale:
                return False
    # interior angle at every corner
    tol = math.radians(angle_tol_deg)
    for i in range(4):
        prev = p[(i - 1) % 4]
        cur = p[i]
        nxt = p[(i + 1) % 4]
        v1 = unit((prev[0] - cur[0], prev[1] - cur[1]))
        v2 = unit((nxt[0] - cur[0], nxt[1] - cur[1]))
        if v1 == (0.0, 0.0) or v2 == (0.0, 0.0):
            return False
        dot = max(-1.0, min(1.0, v1[0] * v2[0] + v1[1] * v2[1]))
        if abs(math.acos(dot) - math.pi / 2.0) > tol:
            return False
    return True


def rect_corners(centre, u, w, h) -> np.ndarray:
    """The four corners of an oriented rectangle, counter-clockwise.

    ``u`` is the unit axis along which the rectangle is ``w`` long; the
    rectangle is ``h`` across it.  The corners are returned in the same
    winding as the loop they came from whenever ``u`` came from that loop,
    so ``is_rectangle(corners)`` holds again.
    """
    cx, cy = float(centre[0]), float(centre[1])
    ux, uy = unit(u)
    nx, ny = -uy, ux
    hw, hh = float(w) / 2.0, float(h) / 2.0
    return np.asarray([
        (cx - ux * hw - nx * hh, cy - uy * hw - ny * hh),
        (cx + ux * hw - nx * hh, cy + uy * hw - ny * hh),
        (cx + ux * hw + nx * hh, cy + uy * hw + ny * hh),
        (cx - ux * hw + nx * hh, cy - uy * hw + ny * hh),
    ], dtype=float)


def rect_from_corners(pts):
    """``(centre, u, w, h)`` for a rectangle given by its four corners.

    ``u`` is taken from the **first edge** of the ring, so a square always
    yields the same orientation no matter how it was drawn, and a
    non-square rectangle yields ``w`` along its own long side only when
    the caller ordered the corners that way — which ``features.py`` always
    does.  ``w`` is the length of edge 0→1 and ``h`` the length of the
    perpendicular edge 1→2.
    """
    p = as_points(pts)
    if len(p) != 4:
        raise ValueError("rect_from_corners needs exactly 4 points")
    c = (float(p[:, 0].mean()), float(p[:, 1].mean()))
    u = unit((p[1, 0] - p[0, 0], p[1, 1] - p[0, 1]))
    w = math.hypot(p[1, 0] - p[0, 0], p[1, 1] - p[0, 1])
    h = math.hypot(p[2, 0] - p[1, 0], p[2, 1] - p[1, 1])
    return c, u, w, h


def segment_axis_params(a, b, origin, u):
    """``(tmin, tmax)`` of segment ``a``–``b`` projected on the axis through
    ``origin`` with unit direction ``u``."""
    ox, oy = float(origin[0]), float(origin[1])
    ux, uy = unit(u)
    t1 = (float(a[0]) - ox) * ux + (float(a[1]) - oy) * uy
    t2 = (float(b[0]) - ox) * ux + (float(b[1]) - oy) * uy
    return (min(t1, t2), max(t1, t2))


# ── clash (SAT) ─────────────────────────────────────────────────────────────

def sat_penetration(a, b) -> tuple[float, np.ndarray] | None:
    """Separating-axis penetration of two **convex** polygons.

    Returns ``(depth, mtv)`` or ``None`` when the polygons do not overlap.
    ``mtv`` is the *minimum translation vector that moves polygon ``a`` out
    of polygon ``b``* — i.e. the direction a MECH item has to move to clear
    a STR one (§1.3 D).

    Both inputs must be convex and given in boundary order; callers pass a
    convex hull when the source polygon is not (and say so in
    ``details.hull_approx``).  The depth is the smallest overlap over every
    edge normal of both polygons, which is the standard 2-D SAT result.
    """
    pa, pb = as_points(a), as_points(b)
    if len(pa) < 3 or len(pb) < 3:
        return None
    best_depth = None
    best_mv = None
    for poly in (pa, pb):
        n = len(poly)
        for i in range(n):
            p = poly[i]
            q = poly[(i + 1) % n]
            nx, ny = -(float(q[1]) - float(p[1])), float(q[0]) - float(p[0])
            L = math.hypot(nx, ny)
            if L < EPS:
                continue
            nx /= L
            ny /= L
            proj_a = pa[:, 0] * nx + pa[:, 1] * ny
            proj_b = pb[:, 0] * nx + pb[:, 1] * ny
            a_min, a_max = float(proj_a.min()), float(proj_a.max())
            b_min, b_max = float(proj_b.min()), float(proj_b.max())
            push_neg = a_max - b_min          # move a in -n
            push_pos = b_max - a_min          # move a in +n
            if push_neg <= 0.0 or push_pos <= 0.0:
                return None                    # separating axis
            if push_neg <= push_pos:
                depth, sx, sy = push_neg, -nx, -ny
            else:
                depth, sx, sy = push_pos, nx, ny
            if best_depth is None or depth < best_depth:
                best_depth = depth
                best_mv = np.asarray([sx * depth, sy * depth], dtype=float)
    if best_depth is None or best_mv is None:
        return None
    return float(best_depth), best_mv


# ── containment ─────────────────────────────────────────────────────────────

def signed_boundary_distances(verts, poly) -> np.ndarray:
    """Signed distance of each vertex to the polygon boundary.

    Positive **outside**, negative inside, zero on the boundary — the sign
    convention of §1.3 C.  The magnitude is always the distance to the
    nearest edge, so an inside point is ``-min(edge distance)``.
    """
    q = as_points(poly)
    v = as_points(verts)
    if len(q) < 3 or len(v) == 0:
        return np.zeros(len(v), dtype=float)
    a = q
    b = np.roll(q, -1, axis=0)
    out = np.empty(len(v), dtype=float)
    for i, p in enumerate(v):
        d = min(point_segment_distance(p, a[k], b[k]) for k in range(len(a)))
        out[i] = -d if point_in_polygon(p, q) else d
    return out


def convex_clip(subject, clip) -> np.ndarray:
    """Sutherland–Hodgman clip of a polygon by a **convex** polygon.

    Returns the (possibly empty) polygon ``subject ∩ clip``.  Both inputs
    must wind the same way; ``as_points`` order is preserved.
    """
    subj = as_points(subject)
    clp = as_points(clip)
    if len(subj) < 3 or len(clp) < 3:
        return np.zeros((0, 2), dtype=float)
    if polygon_area(clp) < 0.0:
        clp = clp[::-1]
    output = [tuple(row) for row in subj]
    n = len(clp)
    for i in range(n):
        if not output:
            return np.zeros((0, 2), dtype=float)
        a, b = clp[i], clp[(i + 1) % n]
        ex, ey = float(b[0]) - float(a[0]), float(b[1]) - float(a[1])
        inputs = output
        output = []
        prev = inputs[-1]
        prev_in = (ex * (prev[1] - float(a[1])) - ey * (prev[0] - float(a[0]))) >= 0.0
        for cur in inputs:
            cur_in = (ex * (cur[1] - float(a[1])) - ey * (cur[0] - float(a[0]))) >= 0.0
            if cur_in:
                if not prev_in:
                    output.append(_isect(prev, cur, a, ex, ey))
                output.append(cur)
            elif prev_in:
                output.append(_isect(prev, cur, a, ex, ey))
            prev, prev_in = cur, cur_in
    return np.asarray(output, dtype=float).reshape(-1, 2)


def _isect(p1, p2, a, ex, ey):
    x1, y1 = p1
    x2, y2 = p2
    d1 = ex * (y1 - a[1]) - ey * (x1 - a[0])
    d2 = ex * (y2 - a[1]) - ey * (x2 - a[0])
    t = d1 / (d1 - d2) if abs(d1 - d2) > EPS else 0.0
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def convex_clip_area(subject, clip) -> float:
    """Area of ``subject ∩ clip`` for a convex ``clip``."""
    poly = convex_clip(subject, clip)
    return abs(polygon_area(poly)) if len(poly) >= 3 else 0.0


def polygon_overlap_fraction(subject, clip) -> float:
    """Fraction of ``subject``'s vertices that fall inside ``clip``.

    §1.3 C uses this for a *non-convex* counterpart outline, where a convex
    clip would be wrong.  Boundary points count as inside.
    """
    v = as_points(subject)
    if len(v) == 0:
        return 0.0
    hits = sum(1 for p in v if point_in_polygon(p, clip))
    return hits / len(v)


# ── rigid fit (used by the bolt pattern check, §1.3 A.3) ────────────────────

def kabsch_rigid(src, dst, weights=None):
    """Least-squares rigid motion (rotation + translation, **no scale**).

    Minimises ``sum w_i | R·s_i + t − d_i |²`` and returns
    ``(R, t, rms)`` where ``R`` is a 2×2 row-vector rotation matrix
    (``p_world = R @ p_local + t``) and ``rms`` is the weighted RMS
    residual in the input units.  A pure reflection in the data is *not*
    undone: this is a rotation-only fit, as §1.3 A.3 requires.
    """
    s = as_points(src)
    d = as_points(dst)
    if len(s) != len(d) or len(s) < 2:
        raise ValueError("kabsch_rigid needs >= 2 matched point pairs")
    w = np.ones(len(s), dtype=float) if weights is None else np.asarray(weights, dtype=float)
    tot = float(w.sum())
    if tot <= 0.0:
        raise ValueError("kabsch_rigid needs a positive total weight")
    cs = (s * w[:, None]).sum(axis=0) / tot
    cd = (d * w[:, None]).sum(axis=0) / tot
    H = ((s - cs) * w[:, None]).T @ (d - cd)
    U, _S, Vt = np.linalg.svd(H)
    R = Vt.T @ U.T
    if np.linalg.det(R) < 0.0:                    # keep it a rotation
        Vt[-1, :] *= -1.0
        R = Vt.T @ U.T
    t = cd - R @ cs
    res = (R @ s.T).T + t - d
    rms = float(math.sqrt(float((w[:, None] * res * res).sum()) / tot))
    return R, t, rms


def rigid_apply(R, t, pts) -> np.ndarray:
    """Apply a row-vector 2-D rigid motion to one point or an ``(N, 2)`` array."""
    arr = as_points(pts)
    return arr @ np.asarray(R, dtype=float).T + np.asarray(t, dtype=float)