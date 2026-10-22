"""core/geom.py — shared geometry helpers used by spotcheck + intra detectors."""

from __future__ import annotations

import math


def point_segment_distance(px: float, py: float,
                           x1: float, y1: float,
                           x2: float, y2: float) -> float:
    """Shortest distance from point P=(px,py) to segment A=(x1,y1)..B=(x2,y2)."""
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = x1 + t * dx, y1 + t * dy
    return math.hypot(px - cx, py - cy)


def segments_intersect(p1, p2, p3, p4) -> bool:
    """True if segments p1-p2 and p3-p4 properly cross (interior crossing)."""
    def ccw(A, B, C):
        return (C[1] - A[1]) * (B[0] - A[0]) > (B[1] - A[1]) * (C[0] - A[0])
    return ccw(p1, p3, p4) != ccw(p2, p3, p4) and \
           ccw(p1, p2, p3) != ccw(p1, p2, p4)


def segment_to_segment_distance(a, b, c, d) -> float:
    """Minimum distance between segments a-b and c-d (0 if they cross)."""
    if segments_intersect(a, b, c, d):
        return 0.0
    return min(
        point_segment_distance(a[0], a[1], c[0], c[1], d[0], d[1]),
        point_segment_distance(b[0], b[1], c[0], c[1], d[0], d[1]),
        point_segment_distance(c[0], c[1], a[0], a[1], b[0], b[1]),
        point_segment_distance(d[0], d[1], a[0], a[1], b[0], b[1]),
    )


def polygon_centroid(pts: list[tuple[float, float]]) -> tuple[float, float]:
    """Centroid of a polygon (vertex list, no repeat). Falls back to mean."""
    n = len(pts)
    if n == 0:
        return (0.0, 0.0)
    if n == 1:
        return pts[0]
    if n == 2:
        return ((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2)
    cx = cy = 0.0
    signed_area = 0.0
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        signed_area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    signed_area *= 0.5
    if abs(signed_area) < 1e-9:
        sx = sum(p[0] for p in pts) / n
        sy = sum(p[1] for p in pts) / n
        return (sx, sy)
    cx /= 6.0 * signed_area
    cy /= 6.0 * signed_area
    return (cx, cy)


def long_axis_lines(container, *, min_length: float = 100.0):
    """Yield long axis-aligned LINE / LWPOLYLINE segments from a container.

    Used by `core/axes.py` to build a per-drawing cache of "potential
    gridlines" — LINE / LWPOLYLINE edges longer than `min_length`
    (default 100 drawing units, matching the magic threshold used by
    `core/fit.py:_is_vertical` / `_is_horizontal`). Closed LWPOLYLINE
    entities are skipped — those are equipment outlines, not gridlines.

    Yields `((x1, y1), (x2, y2))` tuples representing the segment
    endpoints. Cheap; callers should cache the result per drawing.
    """
    for e in list(container):
        t = e.dxftype()
        if t == "LINE":
            try:
                s = e.dxf.start
                end = e.dxf.end
            except Exception:
                continue
            seg_len = math.hypot(end[0] - s[0], end[1] - s[1])
            if seg_len < min_length:
                continue
            yield ((float(s[0]), float(s[1])), (float(end[0]), float(end[1])))
        elif t == "LWPOLYLINE":
            # Skip closed polylines (equipment outlines) and single-vertex
            # artifacts.
            if getattr(e, "closed", False):
                continue
            try:
                verts = list(e.vertices())
            except Exception:
                continue
            if len(verts) < 2:
                continue
            for i in range(len(verts) - 1):
                p, q = verts[i], verts[i + 1]
                try:
                    seg_len = math.hypot(q[0] - p[0], q[1] - p[1])
                except Exception:
                    continue
                if seg_len < min_length:
                    continue
                yield ((float(p[0]), float(p[1])), (float(q[0]), float(q[1])))
