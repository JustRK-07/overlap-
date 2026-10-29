#!/usr/bin/env python3
"""core/mismatch/checks/containment.py — a footprint inside a civil outline.

PLAN_MISMATCH §1.3 C.  A MECH skid (or any other ``mech_skid`` footprint) is
compared with the ``str_outline`` polygon it sits on, and two things can be
wrong:

``OUTSIDE_OUTLINE``
    a corner of the footprint has come out past the plinth.  The magnitude is
    how far out the worst corner is, and the offset points from that corner
    back to the nearest point of the outline — the direction it has to move
    to be back on the footing.
``EDGE_DISTANCE``
    the footprint is inside but too close to the edge for the tolerance it is
    allowed.  The magnitude is the *shortfall* against ``min_edge_mm``, so a
    footprint 30 mm from the edge of a 50 mm rule reads "20 mm short", which
    is what an engineer has to fix, rather than the 30 mm that is already in
    the drawing.

The signed distance to the outline boundary is **positive outside**
(:func:`core.mismatch.geom.signed_boundary_distances`), so one ``max`` decides
both cases and there is no second containment test to keep in step.

Which outline
-------------
A footprint can sit near several outlines.  The one with the largest overlap
is the one it belongs to — measured as a clipped area against a convex
outline, and as the fraction of the footprint's own vertices that fall inside
a non-convex one, so the two are comparable fractions.
"""

from __future__ import annotations

import math

import numpy as np

from core.spatial import GridIndex

from .. import geom
from ..model import Finding, format_message
from . import Env, describe, polygon_of

__all__ = ["run"]


def _overlap_fraction(foot, outline_ring) -> float:
    """How much of ``foot`` the outline covers, in ``[0, 1]``."""
    if geom.is_convex(outline_ring):
        area = abs(geom.polygon_area(foot))
        if area <= geom.EPS:
            return geom.polygon_overlap_fraction(foot, outline_ring)
        return min(1.0, geom.convex_clip_area(foot, outline_ring) / area)
    return geom.polygon_overlap_fraction(foot, outline_ring)


def _choose_outline(env, foot, outlines, index):
    """The outline the footprint belongs on, or ``None``."""
    radius = env.u(env.search_mm(env.policy.search.outline))
    cands = [outlines[int(j)] for j in index.query_bbox(_grown_bbox(foot, radius))]
    best = None
    for pos, out in enumerate(cands):
        ring = polygon_of(out)
        if len(ring) < 3:
            continue
        frac = _overlap_fraction(polygon_of(foot), ring)
        if frac <= 0.0:
            continue
        if best is None or frac > best[0]:
            best = (frac, pos, out, ring)
    return best


def _grown_bbox(foot, pad) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = foot.bbox()
    return (x0 - pad, y0 - pad, x1 + pad, y1 + pad)


def run(env: Env) -> list[Finding]:
    """Every containment finding of one run (§1.3 C)."""
    if not env.policy.checks.containment:
        return []
    findings: list[Finding] = []
    for mech_fam, str_fam in env.policy.pairs.containment:
        foots = [f for f in env.mech_by_family.get(mech_fam, ())
                 if f.kind in ("rect", "polygon")]
        outlines = [f for f in env.str_by_family.get(str_fam, ())
                    if f.kind in ("rect", "polygon")]
        if not foots or not outlines:
            env.track_pair("containment", mech_fam, str_fam, len(foots),
                           len(outlines))
            continue
        index = GridIndex(np.asarray([o.bbox() for o in outlines], dtype=float))
        n_matched = 0
        for foot in sorted(foots, key=lambda f: (round(f.cx, 6), round(f.cy, 6),
                                                 f.handle)):
            got, matched = _footprint(env, foot, outlines, index, mech_fam, str_fam)
            n_matched += 1 if matched else 0
            findings += got
        env.track_pair("containment", mech_fam, str_fam, len(foots),
                       len(outlines), matched_mech=n_matched)
    return findings


def _footprint(env, foot, outlines, index, mech_fam, str_fam):
    """One footprint against the outline it sits on.  ``(findings, had_one)``."""
    pol = env.policy
    mm = env.mm
    ring = polygon_of(foot)
    if len(ring) < 3:
        return [], False
    best = _choose_outline(env, foot, outlines, index)
    if best is None:
        # `requires_counterpart` is false by default: a skid with no civil
        # outline anywhere near it is a drawing, not a defect.
        return [], False
    _frac, _pos, outline, oring = best

    signed = geom.signed_boundary_distances(ring, oring)     # + = outside
    worst = int(np.argmax(signed))
    worst_v = ring[worst]
    worst_d = float(signed[worst])
    name = describe([outline.entity_ref(str_fam)])
    tol = float(pol.tol.outline)

    if worst_d * mm > tol:
        q = _nearest_on_ring(worst_v, oring)
        return [Finding(
            kind="OUTSIDE_OUTLINE", severity="CRITICAL",
            mech_ref=foot.entity_ref(mech_fam), str_ref=outline.entity_ref(str_fam),
            location=(float(worst_v[0]), float(worst_v[1])), str_location=q,
            offset_mm=((q[0] - worst_v[0]) * mm, (q[1] - worst_v[1]) * mm),
            magnitude_mm=worst_d * mm, tolerance_mm=tol,
            details={"vertex": worst, "outside_by_mm": worst_d * mm,
                     "n_vertices": len(ring)},
            message=format_message("OUTSIDE_OUTLINE", mech_block=foot.block,
                                   magnitude=worst_d * mm, str_refs=name,
                                   tolerance=tol))], True

    edge_mm = -worst_d * mm               # the smallest inside distance, in mm
    min_edge = float(pol.tol.min_edge)
    if edge_mm >= min_edge:
        return [], True
    n = _inward_normal(worst_v, oring)
    shortfall = min_edge - edge_mm
    return [Finding(
        kind="EDGE_DISTANCE", severity="WARNING",
        mech_ref=foot.entity_ref(mech_fam), str_ref=outline.entity_ref(str_fam),
        location=(float(worst_v[0]), float(worst_v[1])),
        offset_mm=(n[0] * shortfall, n[1] * shortfall),
        magnitude_mm=shortfall, tolerance_mm=min_edge,
        details={"edge_distance_mm": edge_mm, "vertex": worst,
                 "n_vertices": len(ring)},
        message=format_message("EDGE_DISTANCE", mech_block=foot.block,
                               edge=edge_mm, str_refs=name, tolerance=min_edge))], True


def _nearest_on_ring(pt, ring) -> tuple[float, float]:
    p = (float(pt[0]), float(pt[1]))
    best, bestd = p, math.inf
    for k in range(len(ring)):
        a, b = ring[k], ring[(k + 1) % len(ring)]
        d = geom.point_segment_distance(p, a, b)
        if d < bestd:
            bestd = d
            ax, ay = float(a[0]), float(a[1])
            dx, dy = float(b[0]) - ax, float(b[1]) - ay
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 < geom.EPS else max(
                0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
            best = (ax + t * dx, ay + t * dy)
    return best


def _inward_normal(pt, ring) -> tuple[float, float]:
    """Unit vector pointing into the polygon from its edge nearest ``pt``.

    The polygon is treated as wound counter-clockwise, which is what
    ``features.py`` produces for both rects and closed polylines, so the left
    normal of the nearest edge points inwards.
    """
    best, bestd, dirn = 0.0, math.inf, (0.0, 0.0)
    for k in range(len(ring)):
        a, b = ring[k], ring[(k + 1) % len(ring)]
        d = geom.point_segment_distance(pt, a, b)
        if d < bestd:
            bestd, best = d, k
            dirn = (float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
    nx, ny = -dirn[1], dirn[0]
    L = math.hypot(nx, ny)
    if L < geom.EPS:
        return (0.0, 0.0)
    return (nx / L, ny / L)