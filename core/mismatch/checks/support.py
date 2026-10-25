#!/usr/bin/env python3
"""core/mismatch/checks/support.py — a saddle or a support point on a beam.

PLAN_MISMATCH §1.3 B.  Both legs — a rectangular saddle (a ``mech_support``
rect) and a bare support point (a ``mech_support_pt`` point/insert) — pair
against the same ``str_member`` bands and report against the same tolerance
family; they differ only in how "off" is measured.

The measurement
---------------
A band has a centreline ``a``–``b`` and a half-width ``hw``.  For a saddle
``S`` with centre ``c``:

* ``n`` is the band's left normal and ``u`` its axis;
* ``s_perp`` is the extent of ``S`` measured along ``n`` — how wide the
  saddle is across the beam;
* ``e`` is the signed perpendicular offset of ``c`` from the centreline,
  measured to the *clamped* projection, so a saddle beyond the end of the
  beam is measured along the end rather than along an infinite line;
* ``allowed = |2·hw − s_perp| / 2 + support_tol_mm`` — the formula is
  deliberately asymmetric about a matching width: a narrow saddle on a wide
  flange and a wide saddle spanning a flange are both legitimate, and the
  band of tolerances that says so is ``|2hw − s_perp| / 2``.

``bearing_ratio`` is how much of the saddle still lands on the flange
(``perp_overlap / min(2·hw, s_perp)``); below ``crit_bearing`` the saddle is
barely supported and the finding is CRITICAL rather than WARNING.

A point support is simpler: ``allowed = hw + point_tol_mm`` and the distance
is to the centreline itself.

Everything that comes out here is a **displacement** item for
:mod:`core.mismatch.context`; for supports only the component along the band
normal is the part that means anything, so that is what the neighbour median
is subtracted from.
"""

from __future__ import annotations

import math

import numpy as np

from core.spatial import GridIndex

from ..model import Finding, format_message
from . import Env, describe, extent_along, overlap, polygon_of

__all__ = ["run", "band_candidates"]


def band_candidates(env: Env, support, bands, index) -> list:
    """Bands whose box is within ``search_mm.member`` of ``support``.

    Sorted by ``(centre x, centre y, handle)`` so that a tie in the selection
    score below always resolves to the same band.
    """
    radius = env.u(env.search_mm(env.policy.search.member))
    hits = [bands[int(j)] for j in index.query_radius(support.centre(), radius)]
    hits.sort(key=lambda b: (b.cx, b.cy, b.handle))
    return hits


def _axis_frame(band):
    """``(u, n)`` of a band: its axis and its left normal."""
    u = (float(band.ux), float(band.uy))
    return u, (-u[1], u[0])


def _clamped_projection(pt, band, u):
    """The point of the centreline closest to ``pt``, and the signed offset.

    Clamping to the segment is what makes a saddle that has run off the end
    of the beam measurable: the offset then points along the beam rather than
    into thin air.
    """
    a, b = band.a, band.b
    dx, dy = float(b[0]) - float(a[0]), float(b[1]) - float(a[1])
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 <= 0.0 else max(0.0, min(
        1.0, ((pt[0] - a[0]) * dx + (pt[1] - a[1]) * dy) / L2))
    px, py = float(a[0]) + t * dx, float(a[1]) + t * dy
    n = (-u[1], u[0])
    e = (pt[0] - px) * n[0] + (pt[1] - py) * n[1]
    return (px, py), e


def _pick_band(env, corners, centre, bands, support_tol, normal_of):
    """The band the saddle belongs on, and how it sits on it.

    Selection is §1.3 B's rule: the smallest ``|e| / allowed``, then the
    largest perpendicular overlap, then the first band in the deterministic
    candidate order.
    """
    best = None
    for band in bands:
        u, n = _axis_frame(band)
        lo, hi = extent_along(corners, centre, n)
        s_perp = hi - lo
        _proj, e = _clamped_projection(centre, band, u)
        allowed = abs(2.0 * band.hw - s_perp) / 2.0 + env.u(support_tol)
        perp_overlap = overlap(e - s_perp / 2.0, e + s_perp / 2.0, -band.hw, band.hw)
        bearing = perp_overlap / min(2.0 * band.hw, s_perp) if min(2.0 * band.hw, s_perp) > 0 else 0.0
        key = (abs(e) / allowed if allowed > 0 else math.inf, -perp_overlap)
        if best is None or key < best[0]:
            best = (key, band, u, n, e, allowed, perp_overlap, bearing, s_perp)
    if best is None:
        return None
    _key, band, u, n, e, allowed, perp_overlap, bearing, s_perp = best
    return {"band": band, "u": u, "n": n, "e": e, "allowed": allowed,
            "perp_overlap": perp_overlap, "bearing": bearing, "s_perp": s_perp,
            "normal_of": normal_of}


# ── the check ───────────────────────────────────────────────────────────────

def run(env: Env) -> list[Finding]:
    """Every support finding of one run (§1.3 B)."""
    if not env.policy.checks.support:
        return []
    findings: list[Finding] = []
    for mech_fam, str_fam in env.policy.pairs.support:
        items = env.mech_by_family.get(mech_fam, ())
        bands = env.str_by_family.get(str_fam, ())
        if not items:
            continue
        if not bands:
            env.track("support", len(items), 0)
            continue
        index = GridIndex(np.asarray([b.bbox() for b in bands], dtype=float))
        n_matched = 0
        for item in sorted(items, key=lambda f: (round(f.cx, 6), round(f.cy, 6),
                                                 f.handle)):
            if item.kind in ("point", "insert"):
                got, matched = _point_support(env, item, bands, index, mech_fam, str_fam)
            else:
                got, matched = _rect_support(env, item, bands, index, mech_fam, str_fam)
            n_matched += 1 if matched else 0
            findings += got
        env.track("support", len(items), n_matched)
    return findings


def _missing(env, item, mech_fam, str_fam, search_mm) -> list[Finding]:
    """No member within the search radius — the support has nothing to sit on."""
    return [Finding(
        kind="MISSING_COUNTERPART", severity="CRITICAL",
        mech_ref=item.entity_ref(mech_fam), location=item.centre(),
        tolerance_mm=float(env.policy.tol.support),
        details={"n_candidates": 0, "counterpart": str_fam},
        message=format_message("MISSING_COUNTERPART", mech_block=item.block,
                               counterpart=str_fam, search=search_mm))]


def _rect_support(env, item, bands, index, mech_fam, str_fam):
    """A rectangular saddle on a beam.  ``(findings, had_a_band)``."""
    pol = env.policy
    mm = env.mm
    tol = float(pol.tol.support)
    search_mm = env.search_mm(pol.search.member)
    corners = polygon_of(item)
    if len(corners) < 3:
        corners = np.asarray([item.centre()], dtype=float)
    cands = band_candidates(env, item, bands, index)
    if not cands:
        return _missing(env, item, mech_fam, str_fam, search_mm), False

    pick = _pick_band(env, corners, item.centre(), cands, tol, "perpendicular")
    band = pick["band"]
    e, allowed = pick["e"], pick["allowed"]
    name = describe([band.entity_ref(str_fam)])
    out: list[Finding] = []

    if abs(e) > allowed:
        n = pick["n"]
        # the direction the MECH saddle has to move to sit on the band
        off_mm = (-e * n[0] * mm, -e * n[1] * mm)
        mag = abs(e) * mm
        bearing = pick["bearing"]
        out.append(Finding(
            kind="SUPPORT_OFF_MEMBER",
            severity="CRITICAL" if bearing < float(pol.crit_bearing) else "WARNING",
            mech_ref=item.entity_ref(mech_fam), str_ref=band.entity_ref(str_fam),
            location=item.centre(), offset_mm=off_mm, magnitude_mm=mag,
            tolerance_mm=allowed * mm,
            details={"bearing_ratio": bearing, "allowed_mm": allowed * mm,
                     "assumed_width": bool(band.assumed_width),
                     "perp_overlap_mm": pick["perp_overlap"] * mm,
                     "band_half_width_mm": env.to_mm(band.hw)},
            message=format_message("SUPPORT_OFF_MEMBER", mech_block=item.block,
                                   magnitude=mag, str_refs=name,
                                   allowed=allowed * mm, bearing=bearing)))

    # overhang along the beam: the saddle sticking past an end of the member
    u = pick["u"]
    s_lo, s_hi = extent_along(corners, band.a, u)
    b_lo, b_hi = extent_along([band.a, band.b], band.a, u)
    for over, sign in ((s_hi - b_hi, 1.0), (b_lo - s_lo, -1.0)):
        if over * mm <= tol:
            continue
        out.append(Finding(
            kind="SUPPORT_OFF_MEMBER", severity="WARNING",
            mech_ref=item.entity_ref(mech_fam), str_ref=band.entity_ref(str_fam),
            location=item.centre(),
            offset_mm=(sign * over * u[0] * mm, sign * over * u[1] * mm),
            magnitude_mm=over * mm, tolerance_mm=tol,
            details={"overhang_mm": over * mm, "end": "start" if sign < 0 else "end",
                     "assumed_width": bool(band.assumed_width)},
            message=format_message("SUPPORT_OFF_MEMBER", mech_block=item.block,
                                   magnitude=over * mm, str_refs=name,
                                   allowed=tol, bearing=1.0)))
    return out, True


def _point_support(env, item, bands, index, mech_fam, str_fam):
    """A bare support point on a beam centreline.  ``(findings, had_a_band)``."""
    pol = env.policy
    mm = env.mm
    tol = float(pol.tol.point)
    search_mm = env.search_mm(pol.search.member)
    cands = band_candidates(env, item, bands, index)
    if not cands:
        return _missing(env, item, mech_fam, str_fam, search_mm), False

    best = None
    for band in cands:
        u, _n = _axis_frame(band)
        proj, e_signed = _clamped_projection(item.centre(), band, u)
        allowed = band.hw + env.u(tol)
        e = abs(e_signed)
        key = (e / allowed if allowed > 0 else math.inf, -env.to_mm(band.hw))
        if best is None or key < best[0]:
            best = (key, band, proj, e_signed, e, allowed)
    _key, band, (px, py), e_signed, e, allowed = best
    if e <= allowed:
        return [], True
    name = describe([band.entity_ref(str_fam)])
    return [Finding(
        kind="SUPPORT_POINT_OFF_MEMBER", severity="CRITICAL",
        mech_ref=item.entity_ref(mech_fam), str_ref=band.entity_ref(str_fam),
        location=item.centre(), str_location=(px, py),
        offset_mm=((px - item.cx) * mm, (py - item.cy) * mm),
        magnitude_mm=e * mm, tolerance_mm=allowed * mm,
        details={"outside_by_mm": (e - band.hw) * mm,
                 "assumed_width": bool(band.assumed_width),
                 "sign": 1.0 if e_signed > 0 else -1.0},
        message=format_message("SUPPORT_POINT_OFF_MEMBER", mech_block=item.block,
                               magnitude=e * mm, str_refs=name, allowed=allowed * mm))], True