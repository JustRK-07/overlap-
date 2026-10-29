#!/usr/bin/env python3
"""core/mismatch/checks/clash.py — equipment against a column.

PLAN_MISMATCH §1.3 D.  This is the shortest check of the four because in plan
view it is one question: do these two footprints overlap, and by how much?
The separating-axis test answers both halves at once — the penetration depth
and the minimum translation vector — and the MTV already points the way the
MECH item has to move to clear the STR one, so it is the offset a finding
reports.

Two things are deliberately *not* reported:

``CLEARANCE``
    a gap smaller than ``min_clearance_mm``.  In plan view without elevations
    that is noise — a column clearing a vessel by 10 mm at one level and not at
    another is the normal case — so ``min_clearance_mm`` defaults to 0 and the
    check stays off (PLAN_MISMATCH §1.3 D).
beams
    a plan-view overlap with a floor beam is what equipment *does*: it sits on
    top of one.  Members only clash when ``clash_with_members`` is set.

Non-convex footprints are clipped to their convex hull for the SAT test and
flagged with ``details.hull_approx`` so nobody mistakes a hull result for an
exact one.
"""

from __future__ import annotations

import math

import numpy as np

from core.spatial import GridIndex

from .. import geom
from ..model import Finding, format_message
from . import Env, describe, footprint_bbox, polygon_of

__all__ = ["run"]


def _hull_ring(f):
    """The convex polygon the SAT test runs on, and whether it is a hull."""
    ring = polygon_of(f)
    if len(ring) < 3:
        return ring, False
    if geom.is_convex(ring):
        return ring, False
    return geom.convex_hull(ring), True


def run(env: Env) -> list[Finding]:
    """Every clash finding of one run (§1.3 D)."""
    if not env.policy.checks.clash:
        return []
    findings: list[Finding] = []
    for mech_fam, str_fam in env.policy.pairs.clash:
        items = [f for f in env.mech_by_family.get(mech_fam, ())
                 if f.kind in ("rect", "polygon")]
        others = [f for f in env.str_by_family.get(str_fam, ())
                  if f.kind in ("rect", "polygon")]
        if env.policy.clash_with_members:
            others += list(env.str_by_family.get("str_member", ()))
        if not items or not others:
            env.track_pair("clash", mech_fam, str_fam, len(items), len(others))
            continue
        # `footprint_bbox` (not `Feature.bbox`) — a band's bbox is its
        # centreline, which excludes the half-width the band actually covers.
        index = GridIndex(np.asarray([footprint_bbox(o) for o in others],
                                     dtype=float))
        n_matched = 0
        for item in sorted(items, key=lambda f: (round(f.cx, 6), round(f.cy, 6),
                                                 f.handle)):
            got, matched = _item(env, item, others, index, mech_fam, str_fam)
            n_matched += 1 if matched else 0
            findings += got
        env.track_pair("clash", mech_fam, str_fam, len(items), len(others),
                       matched_mech=n_matched)
    return findings


def _item(env, item, others, index, mech_fam, str_fam):
    """One MECH footprint against every candidate.  ``(findings, had_one)``."""
    mm = env.mm
    tol = float(env.policy.tol.clash)
    min_clear = float(env.policy.tol.min_clearance)
    ring, hull_approx = _hull_ring(item)
    if len(ring) < 3:
        return [], False
    hits = sorted(int(v) for v in index.query_bbox(footprint_bbox(item)))
    if not hits:
        return [], False

    out: list[Finding] = []
    for j in hits:
        other = others[j]
        oring, o_hull = _hull_ring(other)
        if len(oring) < 3:
            continue
        # a band belongs to `str_member`, not to the paired family
        ofam = other.families[0] if other.families else str_fam
        name = describe([other.entity_ref(ofam)])
        sat = geom.sat_penetration(ring, oring)
        if sat is not None:
            depth, mtv = sat
            if depth * mm > tol:
                out.append(Finding(
                    kind="CLASH", severity="CRITICAL",
                    mech_ref=item.entity_ref(mech_fam),
                    str_ref=other.entity_ref(ofam),
                    location=item.centre(),
                    offset_mm=(float(mtv[0]) * mm, float(mtv[1]) * mm),
                    magnitude_mm=depth * mm, tolerance_mm=tol,
                    details={"hull_approx": bool(hull_approx), "str_hull_approx": bool(o_hull)},
                    message=format_message("CLASH", mech_block=item.block,
                                           str_refs=name, magnitude=depth * mm)))
        elif min_clear > 0.0:
            gap = _min_gap(ring, oring)
            if gap * mm < min_clear:
                out.append(Finding(
                    kind="CLEARANCE", severity="WARNING",
                    mech_ref=item.entity_ref(mech_fam),
                    str_ref=other.entity_ref(ofam),
                    location=item.centre(), magnitude_mm=gap * mm,
                    tolerance_mm=min_clear,
                    details={"hull_approx": bool(hull_approx), "str_hull_approx": bool(o_hull)},
                    message=format_message("CLEARANCE", mech_block=item.block,
                                           str_refs=name, magnitude=gap * mm,
                                           tolerance=min_clear)))
    return out, True


def _min_gap(a, b) -> float:
    """Smallest distance between two **disjoint** convex polygons."""
    ea = [tuple(map(float, (a[i], a[(i + 1) % len(a)]))) for i in range(len(a))]
    eb = [tuple(map(float, (b[i], b[(i + 1) % len(b)]))) for i in range(len(b))]
    best = math.inf
    for p in ea:
        for q in eb:
            best = min(best, geom.segment_distance(p, q))
    return float(best)