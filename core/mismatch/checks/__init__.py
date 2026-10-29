#!/usr/bin/env python3
"""core/mismatch/checks — the four geometry checks of PLAN_MISMATCH §1.3.

``bolts``
    concentricity and pattern fit (:mod:`core.mismatch.checks.bolts`)
``support``
    a saddle or a support point on a beam (:mod:`core.mismatch.checks.support`)
``containment``
    a footprint inside a civil outline (:mod:`core.mismatch.checks.containment`)
``clash``
    equipment against a column (:mod:`core.mismatch.checks.clash`)

Every check is a function ``run(env) -> list[Finding]`` and is handed the
same :class:`Env`: the resolved policy, the two :class:`~core.mismatch.features.FeatureSet`
already mapped into **MECH world coordinates**, the millimetre scale and the
:class:`~core.mismatch.context.FitContext`.

Units
-----
A check never converts a millimetre threshold by hand.  ``env.u(mm)`` turns
mm into drawing units and ``env.to_mm(u)`` the other way, so the same code
is right for a millimetre drawing and for a metre one.

Determinism
-----------
Nothing here iterates a ``set`` or a ``dict`` whose order depends on hashing
of floats: features arrive already sorted by ``Feature.sort_key``, spatial
queries return ascending indices, and every greedy loop sorts an explicit
``(key, tie-break)`` tuple.  Two runs of the same pair therefore produce the
same findings in the same order, which is what the ``uid`` content hash and
``rank_findings`` rely on.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .. import geom
from ..classify import Classifier
from ..features import Feature, FeatureSet
from ..model import EntityRef, Finding
from ..policy import MismatchPolicy

__all__ = ["Env", "CHECKS", "polygon_of", "footprint_bbox", "ref_of", "describe",
           "extent_along", "unit_of", "overlap", "TOL_EFF_FLOOR_MM"]

#: The four checks, in the order §1.3 lists them and in the order they run.
CHECKS = ("bolts", "support", "containment", "clash")

#: A search radius is never allowed to grow past this fraction of the median
#: grid spacing (§1.4) — so a tiny or a huge drawing cannot cross-match.
GRID_SPACING_CAP = 0.25

#: ``tolerance_eff`` is never allowed to be exactly zero, so ``ratio`` always
#: has a finite denominator.  1 nm of a millimetre is noise either way.
TOL_EFF_FLOOR_MM = 1e-9


def unit_of(v) -> tuple[float, float]:
    """Unit vector of a 2-sequence; ``(0.0, 0.0)`` when degenerate."""
    x, y = float(v[0]), float(v[1])
    n = math.hypot(x, y)
    return (x / n, y / n) if n > 1e-12 else (0.0, 0.0)


def polygon_of(f: Feature) -> np.ndarray:
    """The ``(N, 2)`` ring a footprint-ish feature actually occupies.

    A rect is its four corners (in the world-mapped order, so the winding is
    counter-clockwise), a polygon its flattened ring, a **band** its rectangle
    (centreline length × 2·half-width).  Anything else is empty.
    """
    if f.kind == "rect":
        return np.asarray(f.corners(), dtype=float)
    if f.kind == "polygon":
        return np.asarray(f.ring(), dtype=float)
    if f.kind == "band":
        return geom.rect_corners(f.centre(), (f.ux, f.uy), f.length(), 2.0 * f.hw)
    return np.zeros((0, 2), dtype=float)


def footprint_bbox(f: Feature) -> tuple:
    """The bbox of a feature's **body**.

    :meth:`Feature.bbox` reports a band's *centreline* — the support check
    measures the distance to the centreline, so it needs that.  The clash
    check needs the band's actual rectangle.
    """
    if f.kind == "band":
        ring = geom.rect_corners(f.centre(), (f.ux, f.uy), f.length(), 2.0 * f.hw)
        return (float(ring[:, 0].min()), float(ring[:, 1].min()),
                float(ring[:, 0].max()), float(ring[:, 1].max()))
    return f.bbox()


def ref_of(f: Feature, family: str) -> EntityRef:
    """``f.entity_ref(family)`` — the addressable reference of one feature."""
    return f.entity_ref(family)


def describe(refs) -> str:
    """A short human name for one or two counterpart features.

    The report and the UI want "the plate Part-PLATE300-H4", not a handle,
    so the innermost block name is used and the layer is the fallback.
    """
    names = []
    for r in refs:
        if r is None:
            continue
        n = r.block or r.layer or r.handle
        if n and n not in names:
            names.append(str(n))
    if not names:
        return "—"
    return names[0] if len(names) == 1 else f"{names[0]} +{len(names) - 1}"


def extent_along(points, origin, direction) -> tuple[float, float]:
    """``(lo, hi)`` of the projection of ``points`` on a directed axis."""
    p = np.asarray(points, dtype=float).reshape(-1, 2)
    o = np.asarray([float(origin[0]), float(origin[1])], dtype=float)
    u = np.asarray([float(direction[0]), float(direction[1])], dtype=float)
    t = (p - o) @ u
    return float(t.min()), float(t.max())


def overlap(lo1, hi1, lo2, hi2) -> float:
    """Length of the intersection of two closed intervals (0 when disjoint)."""
    return max(0.0, min(hi1, hi2) - max(lo1, lo2))


@dataclass
class Env:
    """Everything a check needs, plus the coverage counters it fills in."""

    policy: MismatchPolicy
    mm: float                                       # mm per drawing unit
    mech: FeatureSet
    str: FeatureSet
    clf: Classifier | None = None
    fit: object | None = None                       # FitContext | None
    grid_spacing_mm: float | None = None            # median MECH grid spacing
    coverage: dict = field(default_factory=dict)
    #: Per-family counts: ``{family: {extracted, eligible, matched, unmatched}}``.
    family_coverage: dict = field(default_factory=dict)
    #: How many rule legs had items on **both** sides — the number the
    #: status is decided on.
    covered_pairs: int = 0
    #: How many rule legs the policy has.
    total_pairs: int = 0
    #: What each offset rule found — pairs, ambiguous and unmatched counts.
    rule_notes: dict = field(default_factory=dict)
    #: The two drawings and the matrices they are read through — an offset
    #: rule may select by **layer**, and a layer is a property of the
    #: drawing, not of the classified features.
    mech_doc: object = None
    str_doc: object = None
    mech_root: object = None
    str_root: object = None

    def __post_init__(self) -> None:
        self.mm = float(self.mm) or 1.0
        self.mech_by_family = self.mech.by_family()
        self.str_by_family = self.str.by_family()
        self.coverage = {name: {"items": 0, "matched": 0, "unmatched": 0}
                         for name in CHECKS}
        self.family_coverage = {}
        for fam, items in list(self.mech_by_family.items()) + \
                list(self.str_by_family.items()):
            c = self.family_coverage.setdefault(
                fam, {"extracted": 0, "eligible": 0, "matched": 0,
                      "unmatched": 0})
            c["extracted"] += len(items)

    # ── units ────────────────────────────────────────────────────────
    def u(self, mm_value) -> float:
        """Millimetres → drawing units."""
        return float(mm_value) / self.mm

    def to_mm(self, units) -> float:
        """Drawing units → millimetres."""
        return float(units) * self.mm

    def tol_eff_mm(self, tol_mm, xy) -> float:
        """The effective tolerance at ``xy`` (fit uncertainty included)."""
        base = float(tol_mm)
        if self.fit is not None:
            base = float(self.fit.tol_eff(base, xy))
        return max(base, TOL_EFF_FLOOR_MM)

    # ── search radii (§1.3 / §1.4) ───────────────────────────────────
    def search_u(self, r_mm: float) -> float:
        """A search radius in units, capped at 0.25 × the grid spacing."""
        r = float(r_mm)
        if self.grid_spacing_mm:
            r = min(r, GRID_SPACING_CAP * float(self.grid_spacing_mm))
        return self.u(r)

    def search_mm(self, r_mm: float) -> float:
        """The same cap, but reported back in millimetres."""
        return self.search_u(r_mm) * self.mm

    # ── coverage ─────────────────────────────────────────────────────
    def track(self, check: str, items: int, matched: int) -> None:
        c = self.coverage.setdefault(check, {"items": 0, "matched": 0, "unmatched": 0})
        c["items"] += int(items)
        c["matched"] += int(matched)
        c["unmatched"] += int(items) - int(matched)
    def track_pair(self, check: str, mech_fam: str, str_fam: str,
                   n_mech: int, n_str: int,
                   matched_mech: int = 0, matched_str: int | None = None) -> None:
        """One rule leg: ``mech_fam`` items against ``str_fam`` items.

        A leg with an **empty side** was never really checked — the two
        drawings had nothing to compare — so its items are *extracted* but
        not *eligible*, and the leg does not count towards the coverage the
        status is decided on.  That distinction is the whole point: a rule
        that never compared anything must not be able to report ``passed``.

        ``matched_mech`` / ``matched_str`` are how many items of each side
        found a counterpart.  They default to the same number, which is
        right for every rule that pairs one item with one item.
        """
        n_mech = int(n_mech or 0)
        n_str = int(n_str or 0)
        matched_mech = int(matched_mech or 0)
        matched_str = (matched_mech if matched_str is None
                       else int(matched_str or 0))
        covered = bool(n_mech) and bool(n_str)
        if covered:
            self.covered_pairs += 1
        for fam, n, matched in ((mech_fam, n_mech, matched_mech),
                                (str_fam, n_str, matched_str)):
            c = self.family_coverage.setdefault(
                fam, {"extracted": 0, "eligible": 0, "matched": 0,
                      "unmatched": 0})
            if covered:
                c["eligible"] += n
                c["matched"] += matched
        self.track(check, n_mech, matched_mech)
