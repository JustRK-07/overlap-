#!/usr/bin/env python3
"""core/mismatch/checks/bolts.py — bolt/hole concentricity and pattern fit.

PLAN_MISMATCH §1.3 A, in the order the plan states it:

1. **Patterns.**  MECH bolt circles are grouped into connected components by
   single link: two bolts link when they lie within ``search_mm.pattern_eps``
   *and* their radii agree within 10 %, so a nut never welds two bolt groups
   together.  A link never crosses a root INSERT, so a bolt group drawn twice
   stays two patterns.  Patterns come out sorted by
   ``(centroid x, centroid y, first handle)``.

2. **Per-bolt search radius** ``R_b = min(search_mm.bolt,
   0.45 × the pattern's own minimum bolt spacing, 0.25 × median grid spacing)``
   and the greedy one-to-one assignment of §1.4's cost
   ``d/R + w_size·|ln(r_a/r_b)|``.

3. **Rigid fit** — rotation and translation, *no* scale — by RANSAC-style
   enumeration of minimal two-point sets in index order.  Hypotheses are
   scored by ``(-n_inliers, median inlier residual, enumeration index)``, a
   total order that uses no RNG, so the winner is reproducible run to run.

4. **Hierarchy.**  The pattern is judged first, as a whole (translation *and*
   rotation); an individual bolt is reported only when its residual under that
   fit is still too large.  One defect therefore yields one finding, and a
   rotated eight-bolt flange is never eight findings.

Conventions
-----------
``offset_mm`` is **STR − MECH**: the direction the MECH item has to move to
reach its counterpart.  The single exception is ``BOLT_OFF_PLATE``, which has
no STR counterpart to point at and reports the *overhang* instead — from the
nearest plate-edge point out to the bolt — so the arrow leaves the plate and
points at the bolt that has come off it.
"""

from __future__ import annotations

import math

import numpy as np

from core.spatial import GridIndex

from .. import geom
from ..model import Finding, format_message
from . import Env, describe, polygon_of

__all__ = ["run", "group_patterns", "rigid_fit"]

#: Weight of the size term in the §1.4 assignment cost.
W_SIZE = 1.0

#: Two bolts only link into one pattern when their radii agree this closely.
RADIUS_LINK_TOL = 0.10

#: A pattern with more bolts than this is subsampled before enumeration.
MAX_FIT_BOLTS = 32

#: A runner-up within this fraction of the best cost makes a match ambiguous.
AMBIGUITY_FRAC = 0.10

#: How far past the pattern's own radius the plate fallback looks (§1.3 A.7).
PLATE_FALLBACK_MARGIN_MM = 300.0


# ── 1. patterns ─────────────────────────────────────────────────────────────

def _radius(f) -> float:
    return float(f.r) if f.r is not None else 0.0


def group_patterns(bolts, eps_units) -> list[list]:
    """Single-link components of MECH bolts, in a deterministic order.

    Two bolts link when they are no further apart than ``eps_units`` **and**
    their radii agree within :data:`RADIUS_LINK_TOL`.  Bolts under different
    root INSERTs never link, so the same block drawn twice stays two groups.

    The neighbours are found with a spatial index rather than by scanning
    every pair: two bolts can only link when they are within
    ``eps_units`` of each other, so only those have to be compared.  The
    all-pairs scan was O(n²) — 200 million comparisons for the 20 000
    bolts of the performance fixture, and the bolt check alone took 74 of
    the 90 second budget.
    """
    n = len(bolts)
    if n == 0:
        return []
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    radii = [_radius(b) for b in bolts]
    max_r = max(radii) if radii else 0.0
    boxes = np.asarray(
        [(b.cx - r, b.cy - r, b.cx + r, b.cy + r)
         for b, r in zip(bolts, radii)], dtype=float)
    # a cell of eps_units keeps ~1 bolt per bucket, so a query returns a
    # handful of candidates instead of the whole drawing
    cell = float(eps_units) if eps_units and eps_units > 0.0 else None
    index = GridIndex(boxes, cell=cell)

    for i in range(n):
        cand = index.query_radius((bolts[i].cx, bolts[i].cy),
                                  eps_units + max_r)
        for j in cand:
            j = int(j)
            if j <= i:
                continue
            if bolts[i].root_block != bolts[j].root_block:
                continue
            ri, rj = radii[i], radii[j]
            if abs(ri - rj) > RADIUS_LINK_TOL * max(ri, rj, 1e-9):
                continue
            d = math.hypot(bolts[i].cx - bolts[j].cx, bolts[i].cy - bolts[j].cy)
            if d <= eps_units:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out = [[bolts[i] for i in sorted(m, key=lambda i: (bolts[i].cx, bolts[i].cy,
                                                      bolts[i].handle))]
           for m in groups.values()]
    out.sort(key=lambda g: (float(np.mean([b.cx for b in g])),
                            float(np.mean([b.cy for b in g])), g[0].handle))
    return out


def _min_spacing(pattern) -> float:
    """Smallest centre-to-centre distance inside a pattern (``inf`` if one bolt)."""
    if len(pattern) < 2:
        return math.inf
    best = math.inf
    for i in range(len(pattern)):
        for j in range(i + 1, len(pattern)):
            best = min(best, math.hypot(pattern[i].cx - pattern[j].cx,
                                         pattern[i].cy - pattern[j].cy))
    return best


def _pattern_radius(pattern) -> float:
    """Largest distance of any bolt from the pattern centroid, in units."""
    cx = float(np.mean([b.cx for b in pattern]))
    cy = float(np.mean([b.cy for b in pattern]))
    return max(math.hypot(b.cx - cx, b.cy - cy) for b in pattern)


# ── 2. candidates and the greedy assignment ─────────────────────────────────

def _size_cost(bolt, hole) -> float:
    """``w_size · |ln(r_a / r_b)|``; 0 when either radius is unknown."""
    ra, rb = _radius(bolt), _radius(hole)
    if ra <= 0.0 or rb <= 0.0:
        return 0.0
    return W_SIZE * abs(math.log(ra / rb))


def _assign(bolts, holes, index, radius):
    """Greedy one-to-one assignment — §1.3 A.2 / §1.4.

    Returns ``(pairs, ambiguous)``: ``pairs`` is a list of
    ``(bolt index, hole index, distance)`` ordered by bolt index, and
    ``ambiguous`` holds the bolt indices whose runner-up candidate cost came
    within 10 % of the best one.

    A candidate has to be **strictly** inside ``radius``.  A counterpart that
    lands exactly on the search boundary is not "within" it, and a bolt that
    far out is not concentric with a hole any more — it has come off the
    pattern, which is a different finding with a different message and a
    different fix.
    """
    scored: list[tuple[float, float, int, int]] = []
    ambiguous: set[int] = set()
    for i, b in enumerate(bolts):
        rows = []
        for j in index.query_radius((b.cx, b.cy), radius):
            j = int(j)
            d = math.hypot(holes[j].cx - b.cx, holes[j].cy - b.cy)
            if d >= radius:
                continue
            rows.append((d / radius + _size_cost(b, holes[j]), d, j))
        rows.sort()
        if not rows:
            continue
        if len(rows) > 1 and rows[1][0] <= rows[0][0] * (1.0 + AMBIGUITY_FRAC):
            ambiguous.add(i)
        for cost, d, j in rows:
            scored.append((cost, d, i, j))
    scored.sort()

    taken_b: set[int] = set()
    taken_h: set[int] = set()
    pairs = []
    for _cost, d, i, j in scored:
        if i in taken_b or j in taken_h:
            continue
        taken_b.add(i)
        taken_h.add(j)
        pairs.append((i, j, d))
    pairs.sort(key=lambda p: (p[0], p[1]))
    return pairs, ambiguous


# ── 3. the rigid fit ────────────────────────────────────────────────────────

def _rigid_two(p0, q0, p1, q1):
    """The rotation + translation a two-point hypothesis implies (no scale).

    A rigid motion cannot match two points whose distances disagree, so the
    rotation only *aligns* the two segments and any length difference shows up
    as a residual on the second pair — which is exactly what keeps a scale
    error visible to the fit instead of letting it be absorbed.
    """
    sp = np.asarray(p1, dtype=float) - np.asarray(p0, dtype=float)
    sq = np.asarray(q1, dtype=float) - np.asarray(q0, dtype=float)
    ns = math.hypot(float(sp[0]), float(sp[1]))
    nq = math.hypot(float(sq[0]), float(sq[1]))
    if ns < geom.EPS or nq < geom.EPS:
        return None
    ang = (math.atan2(float(sq[1]), float(sq[0]))
           - math.atan2(float(sp[1]), float(sp[0])))
    c, s = math.cos(ang), math.sin(ang)
    R = np.asarray([[c, -s], [s, c]], dtype=float)
    t = np.asarray(q0, dtype=float) - R @ np.asarray(p0, dtype=float)
    return R, t


def rigid_fit(src, dst, tol):
    """RANSAC-style rigid fit of ``src → dst`` with inliers at ``tol``.

    Minimal sets are every unordered pair of correspondences in index order
    (subsampled every ``ceil(k / 32)``-th when there are more than 32); the
    winner has the most inliers, then the lowest median inlier residual, then
    the first in enumeration order.  Fewer than two inliers falls back to the
    least-squares fit over all pairs.  Returns ``(R, t, residuals, inlier_mask)``
    in the column-vector convention ``p' = R @ p + t``.
    """
    s = geom.as_points(src)
    d = geom.as_points(dst)
    k = len(s)
    if k < 2:
        raise ValueError("a rigid fit needs at least two correspondences")
    step = max(1, int(math.ceil(k / MAX_FIT_BOLTS)))
    idx = list(range(0, k, step))

    best = None
    for ii in range(len(idx)):
        for jj in range(ii + 1, len(idx)):
            i, j = idx[ii], idx[jj]
            hyp = _rigid_two(s[i], d[i], s[j], d[j])
            if hyp is None:
                continue
            R, t = hyp
            res = np.hypot(*(geom.rigid_apply(R, t, s) - d).T)
            mask = res <= tol
            if int(mask.sum()) < 2:
                continue
            key = (-int(mask.sum()), float(np.median(res[mask])), ii, jj)
            if best is None or key < best[0]:
                best = (key, mask)
    if best is not None and int(best[1].sum()) >= 2:
        R, t, _rms = geom.kabsch_rigid(s[best[1]], d[best[1]])
    else:
        R, t, _rms = geom.kabsch_rigid(s, d)
    res = np.hypot(*(geom.rigid_apply(R, t, s) - d).T)
    return R, t, res, res <= tol


def _rotation_deg(R) -> float:
    """CCW rotation of a 2×2 rotation matrix, in degrees."""
    return math.degrees(math.atan2(float(R[1, 0]), float(R[0, 0])))


def _pair_tol_mm(bolt, hole, tol_abs, tol_default, mm) -> float:
    """§1.3 A.4 — radius mismatch plus the absolute concentricity allowance.

    The radius difference is a *length in drawing units* and ``tol_abs`` /
    ``tol_default`` are millimetres, so the difference is converted first; a
    drawing in metres would otherwise get a 0.001 mm allowance instead of a
    1 mm one.
    """
    if bolt.r is None or hole.r is None:
        return float(tol_default)
    return abs(float(hole.r) - float(bolt.r)) * float(mm) + float(tol_abs)


# ── the check ───────────────────────────────────────────────────────────────

def run(env: Env) -> list[Finding]:
    """Every bolt/hole finding of one run (§1.3 A)."""
    if not env.policy.checks.bolts:
        return []
    out: list[Finding] = []
    holes_taken: set[tuple[str, int]] = set()
    for mech_fam, str_fam in env.policy.pairs.bolts:
        bolts = [f for f in env.mech_by_family.get(mech_fam, ()) if f.kind == "circle"]
        holes = [f for f in env.str_by_family.get(str_fam, ()) if f.kind == "circle"]
        if not bolts or not holes:
            # A family nothing was recognised in was never really checked, so
            # it is recorded in the coverage and stays quiet: a
            # MISSING_COUNTERPART per bolt for a drawing that simply has no
            # counterpart family would drown the report (PLAN_MISMATCH §4).
            env.track_pair("bolts", mech_fam, str_fam, len(bolts), len(holes))
            continue
        out += _pairing(env, bolts, holes, mech_fam, str_fam, holes_taken)
    if env.policy.checks.extra_hole:
        out += _extra_holes(env, holes_taken)
    return out


def _pairing(env: Env, bolts, holes, mech_fam, str_fam, holes_taken) -> list[Finding]:
    """One whole ``mech_fam ↔ str_fam`` leg of the bolt check."""
    pol = env.policy
    index = GridIndex(np.asarray([h.bbox() for h in holes], dtype=float))
    plates = [f for f in env.str_by_family.get(pol.pairs.plate_fallback, ())
              if f.kind in ("rect", "polygon")]
    plate_index = (GridIndex(np.asarray([p.bbox() for p in plates], dtype=float))
                   if plates else None)

    findings: list[Finding] = []
    n_assigned = 0
    n_bolts_matched = 0
    n_holes_matched = 0
    for pattern in group_patterns(bolts, env.u(pol.search.pattern_eps)):
        found, matched, n_bolts, n_holes = _judge_pattern(
            env, pattern, holes, index, plates, plate_index, mech_fam, str_fam,
            holes_taken)
        findings += found
        n_assigned += matched
        n_bolts_matched += n_bolts
        n_holes_matched += n_holes
    env.track_pair("bolts", mech_fam, str_fam, len(bolts), len(holes),
                   matched_mech=n_bolts_matched, matched_str=n_holes_matched)
    return findings


def _judge_pattern(env: Env, pattern, holes, index, plates, plate_index,
                   mech_fam, str_fam, holes_taken):
    """Fit one pattern, then report whatever the fit does not explain."""
    pol = env.policy
    mm = env.mm
    tol_abs = float(pol.tol.concentric_abs)
    tol_default = float(pol.tol.concentric_default)

    # §1.3 A.2 — the per-bolt search radius, capped by the drawing itself.
    r_mm = min(float(pol.search.bolt),
               0.45 * env.to_mm(_min_spacing(pattern)),
               env.search_mm(pol.search.bolt))
    radius = env.u(r_mm)

    pairs, ambiguous = _assign(pattern, holes, index, radius)
    tols = [_pair_tol_mm(pattern[i], holes[j], tol_abs, tol_default, mm)
            for i, j, _d in pairs]
    tol_mm = min(tols) if tols else tol_default
    tol_units = env.u(tol_mm)
    block = pattern[0].block or pattern[0].layer
    str_refs = describe([holes[j].entity_ref(str_fam) for _i, j, _d in pairs])

    for _i, j, _d in pairs:
        holes_taken.add((str_fam, j))

    findings: list[Finding] = []
    fitted = len(pattern) >= 2 and len(pairs) >= 2
    R = t = res = inliers = None
    if fitted:
        src = [(pattern[i].cx, pattern[i].cy) for i, _j, _d in pairs]
        dst = [(holes[j].cx, holes[j].cy) for _i, j, _d in pairs]
        try:
            R, t, res, inliers = rigid_fit(src, dst, tol_units)
        except (ValueError, np.linalg.LinAlgError):        # pragma: no cover
            R, t = np.eye(2), np.zeros(2)
            res = np.asarray([math.hypot(q[0] - p[0], q[1] - p[1])
                              for p, q in zip(src, dst)])
            inliers = res <= tol_units
        # §1.3 A.5 — the pattern as a whole, before any single bolt.
        findings += _pattern_finding(env, pattern, pairs, holes, R, t, res, inliers,
                                     mech_fam, str_fam, tol_mm, block, str_refs)

    # §1.3 A.6 — what the pattern fit did not explain.
    for k, (i, j, _d) in enumerate(pairs):
        if fitted and bool(inliers[k]):
            continue
        bolt, hole = pattern[i], holes[j]
        off = (hole.cx - bolt.cx, hole.cy - bolt.cy)
        mag = float(math.hypot(off[0], off[1])) * mm
        tol = tols[k]
        if mag <= tol:
            continue
        findings.append(Finding(
            kind="BOLT_HOLE_OFFSET", severity="CRITICAL",
            mech_ref=bolt.entity_ref(mech_fam), str_ref=hole.entity_ref(str_fam),
            location=bolt.centre(), str_location=hole.centre(),
            offset_mm=(off[0] * mm, off[1] * mm), magnitude_mm=mag,
            tolerance_mm=tol,
            details={"mech_index": i + 1, "n_bolts": len(pattern),
                     "n_assigned": len(pairs), "ambiguous": i in ambiguous},
            message=format_message(
                "BOLT_HOLE_OFFSET", mech_block=block, mech_index=i + 1,
                magnitude=mag, tolerance=tol,
                str_block=hole.block or hole.layer),
        ))

    # §1.3 A.7 — bolts that got no hole at all.
    assigned = {i for i, _j, _d in pairs}
    loose = [idx for idx in range(len(pattern)) if idx not in assigned]
    if loose:
        if pairs:
            findings += [_missing_hole(env, pattern[idx], mech_fam, block, idx,
                                       len(pattern), str_refs, r_mm, tol_mm)
                         for idx in loose]
        else:
            findings += _plate_fallback(env, pattern, loose, mech_fam, plates,
                                        plate_index, r_mm)
    holes_used = {j for _i, j, _d in pairs}
    return findings, len(pairs), len(assigned), len(holes_used)


def _pattern_finding(env, pattern, pairs, holes, R, t, res, inliers,
                     mech_fam, str_fam, tol_mm, block, str_refs) -> list[Finding]:
    """§1.3 A.5 — PATTERN_OFFSET / PATTERN_ROTATED / PATTERN_MISFIT."""
    mm = env.mm
    used = [k for k in range(len(pairs)) if bool(inliers[k])] or list(range(len(pairs)))
    c = np.asarray([float(np.mean([pattern[pairs[k][0]].cx for k in used])),
                    float(np.mean([pattern[pairs[k][0]].cy for k in used]))],
                   dtype=float)
    tc = R @ c + t - c
    shift = float(math.hypot(tc[0], tc[1])) * mm
    theta = _rotation_deg(R)
    r_max = max(math.hypot(b.cx - c[0], b.cy - c[1]) for b in pattern) * mm
    d_rot = 2.0 * r_max * math.sin(math.radians(abs(theta)) / 2.0)
    moved = R @ c + t

    off_bad, rot_bad = shift > tol_mm, d_rot > tol_mm
    if not (off_bad or rot_bad):
        return []
    base = dict(mech_ref=pattern[pairs[0][0]].entity_ref(mech_fam),
                str_ref=holes[pairs[0][1]].entity_ref(str_fam),
                location=(float(c[0]), float(c[1])),
                str_location=(float(moved[0]), float(moved[1])),
                offset_mm=(float(tc[0] * mm), float(tc[1] * mm)),
                tolerance_mm=tol_mm,
                details={"n_bolts": len(pattern), "n_assigned": len(pairs),
                         "n_inliers": int(sum(1 for m in inliers if m)),
                         "rotation_deg": theta, "str_refs": str_refs})
    if off_bad and not rot_bad:
        return [Finding(kind="PATTERN_OFFSET", severity="CRITICAL",
                        magnitude_mm=shift, **base,
                        message=format_message("PATTERN_OFFSET", mech_block=block,
                                               n_bolts=len(pattern), magnitude=shift,
                                               tolerance=tol_mm))]
    if rot_bad and not off_bad:
        return [Finding(kind="PATTERN_ROTATED", severity="CRITICAL",
                        magnitude_mm=d_rot, rotation_deg=theta, **base,
                        message=format_message("PATTERN_ROTATED", mech_block=block,
                                               n_bolts=len(pattern), rotation=theta,
                                               tolerance=tol_mm))]
    return [Finding(kind="PATTERN_MISFIT", severity="CRITICAL",
                    magnitude_mm=max(float(res[k]) * mm for k in range(len(pairs))),
                    rotation_deg=theta, **base,
                    message=format_message("PATTERN_MISFIT", mech_block=block,
                                           n_bolts=len(pattern), magnitude=shift,
                                           rotation=theta, str_refs=str_refs))]


def _missing_hole(env, bolt, mech_fam, block, index, n_bolts, str_refs,
                  search_mm, tol_mm) -> Finding:
    """§1.3 A.7 — the pattern has holes; this bolt did not match one."""
    return Finding(
        kind="MISSING_HOLE", severity="CRITICAL",
        mech_ref=bolt.entity_ref(mech_fam), location=bolt.centre(),
        magnitude_mm=None, tolerance_mm=tol_mm,
        details={"mech_index": index + 1, "n_bolts": n_bolts},
        message=format_message("MISSING_HOLE", mech_block=block, mech_index=index + 1,
                               str_refs=str_refs or "the plate", search=search_mm))


# ── the plate fallback ──────────────────────────────────────────────────────

def _plate_fallback(env, pattern, loose, mech_fam, plates, plate_index,
                    search_mm) -> list[Finding]:
    """§1.3 A.7 — no hole at all: is there a plate, and is the bolt still on it?"""
    mm = env.mm
    radius = env.u(env.to_mm(_pattern_radius(pattern)) + PLATE_FALLBACK_MARGIN_MM)
    cx = float(np.mean([b.cx for b in pattern]))
    cy = float(np.mean([b.cy for b in pattern]))
    hits = []
    if plate_index is not None:
        hits = sorted((plates[int(j)] for j in plate_index.query_radius((cx, cy), radius)),
                      key=lambda p: (p.cx, p.cy, p.handle))
    plate = hits[0] if hits else None

    if plate is None:
        return [Finding(
            kind="MISSING_COUNTERPART", severity="CRITICAL",
            mech_ref=pattern[0].entity_ref(mech_fam),
            location=(cx, cy), tolerance_mm=float(env.policy.tol.concentric_default),
            details={"n_bolts": len(pattern)},
            message=format_message("MISSING_COUNTERPART", mech_block=pattern[0].block,
                                   counterpart=env.policy.pairs.plate_fallback,
                                   search=search_mm))]

    ring = polygon_of(plate)
    ref = plate.entity_ref(env.policy.pairs.plate_fallback)
    name = describe([ref])
    edge_min = float(env.policy.tol.bolt_edge_min)
    out: list[Finding] = []
    for index in loose:
        bolt = pattern[index]
        px, py = _nearest_on_ring((bolt.cx, bolt.cy), ring)
        d_edge = math.hypot(bolt.cx - px, bolt.cy - py) * mm
        inside = geom.point_in_polygon((bolt.cx, bolt.cy), ring)
        # the overhang direction: plate edge -> bolt, so the arrow leaves the
        # plate and points at the bolt that came off it
        base = dict(mech_ref=bolt.entity_ref(mech_fam), str_ref=ref,
                    location=bolt.centre(), str_location=(px, py),
                    offset_mm=((bolt.cx - px) * mm, (bolt.cy - py) * mm))
        extra = {"mech_index": index + 1, "n_bolts": len(pattern)}
        if not inside:
            out.append(Finding(
                kind="BOLT_OFF_PLATE", severity="CRITICAL", magnitude_mm=d_edge,
                tolerance_mm=0.0, **{**base, "details": {**extra,
                                                         "outside_by_mm": d_edge}},
                message=format_message("BOLT_OFF_PLATE", mech_block=bolt.block,
                                       mech_index=index + 1, magnitude=d_edge,
                                       str_refs=name)))
        elif d_edge < edge_min:
            shortfall = edge_min - d_edge
            out.append(Finding(
                kind="BOLT_OFF_PLATE", severity="WARNING", magnitude_mm=shortfall,
                tolerance_mm=edge_min, **{**base, "details": {**extra,
                                                              "edge_distance_mm": d_edge}},
                message=format_message("BOLT_OFF_PLATE", mech_block=bolt.block,
                                       mech_index=index + 1, magnitude=shortfall,
                                       str_refs=name)))
    return out


def _nearest_on_ring(pt, ring) -> tuple[float, float]:
    """The point of the closed ring ``ring`` closest to ``pt``."""
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


# ── EXTRA_HOLE (off by default) ─────────────────────────────────────────────

def _extra_holes(env: Env, holes_taken) -> list[Finding]:
    """§1.3 A.8 — an STR hole no bolt claimed inside a matched pattern's reach."""
    out: list[Finding] = []
    seen: set[tuple[str, int]] = set()
    for mech_fam, str_fam in env.policy.pairs.bolts:
        holes = [f for f in env.str_by_family.get(str_fam, ()) if f.kind == "circle"]
        bolts = [f for f in env.mech_by_family.get(mech_fam, ()) if f.kind == "circle"]
        if not holes or not bolts:
            continue
        index = GridIndex(np.asarray([h.bbox() for h in holes], dtype=float))
        for pattern in group_patterns(bolts, env.u(env.policy.search.pattern_eps)):
            xs = [b.cx for b in pattern]
            ys = [b.cy for b in pattern]
            pad = env.u(env.policy.search.bolt)
            for j in index.query_bbox((min(xs) - pad, min(ys) - pad,
                                      max(xs) + pad, max(ys) + pad)):
                j = int(j)
                if (str_fam, j) in holes_taken or (str_fam, j) in seen:
                    continue
                seen.add((str_fam, j))
                out.append(Finding(
                    kind="EXTRA_HOLE", severity="INFO",
                    str_ref=holes[j].entity_ref(str_fam),
                    location=holes[j].centre(),
                    details={"n_bolts": len(pattern)},
                    message=format_message("EXTRA_HOLE", str_block=holes[j].block or "",
                                           str_refs=holes[j].layer or "—",
                                           search=env.policy.search.bolt)))
    return out