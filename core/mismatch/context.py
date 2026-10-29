#!/usr/bin/env python3
"""core/mismatch/context.py — fit quality vs local mismatch (PLAN_MISMATCH §3).

A tolerance is only as good as the transform it is measured through.  If the
grid fit wobbled by 8 mm *over there*, then an 8 mm discrepancy over there is
the fit, not the steel — so every tolerance in this stage is widened locally
by how uncertain the transform is at that point:

``sigma_at(p) = max(rms_residual, IDW(k=3, power 2) of the leave-one-out residuals)``
``tol_eff = hypot(tol, k_fit · sigma_at(p))``

The second half of §3 is the other side of the same coin.  A displacement is
*local* when it stands out from its neighbours and *systematic* when the whole
neighbourhood moved together — one bolt 5 mm off is a defect, sixteen bolts
5 mm off in the same direction is a datum.  So for each displacement item the
component-wise median of the offsets of its neighbours within ``radius_mm`` is
subtracted, and what is left is the part the neighbours do not explain.

Units
-----
Residuals come out of ``transform.json`` in **MECH drawing units** and are
stored here in millimetres, because every tolerance in the plan is a
millimetre.  Correspondence *points* stay in drawing units, so a neighbour
query can go straight into a :class:`~core.spatial.GridIndex` without a
conversion; inverse-distance weighting is scale-free, so mixing the two in
the interpolation is exact.

The global-median fallback
--------------------------
§3 says that an item with too few neighbours falls back to the *global* median
offset.  Taken literally that is wrong often enough to matter: a drawing with
one 6 mm bolt error among five unrelated displacements would have that one
subtracted away and reported as "nothing", because the median of the other
four happens to be near zero.

The fallback is therefore **guarded**: it is only used when at least
``context.systematic_min_fraction`` of all displacement items already lie
within ``tol_eff`` of the global median — that is, when the drawing really is
shifted as a whole.  Otherwise the median of the neighbourhood is ``(0, 0)``
and the item keeps its full offset.  Both the fixture presets and the
one-systematic-offset case come out right, and the guard is a single
documented deviation rather than a silent one.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from core.spatial import GridIndex

from .model import Finding, format_message

__all__ = ["FitContext", "Displacement", "analyse", "SYSTEMATIC_REASON_LOCAL",
           "SYSTEMATIC_REASON_FIT"]

#: Neighbours used by the inverse-distance uncertainty estimate (§3).
IDW_K = 3

#: Exponent of the inverse-distance weights.
IDW_POWER = 2

#: Kinds whose ``offset_mm`` is a real displacement (PATTERN_ROTATED is not —
#: its magnitude is a *rotation distance*, and there is no direction to share
#: with a neighbour).
DISPLACEMENT_KINDS = ("PATTERN_OFFSET", "PATTERN_MISFIT", "BOLT_HOLE_OFFSET",
                      "SUPPORT_OFF_MEMBER", "SUPPORT_POINT_OFF_MEMBER")

SYSTEMATIC_REASON_LOCAL = ("grids agree; MECH items are consistently shifted by "
                           "{magnitude:.1f} mm — likely drawing-wide "
                           "datum/offset discrepancy")
SYSTEMATIC_REASON_FIT = ("consistent with fit residual — check grid/fit")


# ── the fit context ─────────────────────────────────────────────────────────

@dataclass
class FitContext:
    """What ``transform.json`` says about how much to trust the alignment."""

    points: np.ndarray = field(default_factory=lambda: np.zeros((0, 2), dtype=float))
    loo: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=float))
    rms: float = 0.0
    status: str = "ok"
    warnings: tuple = ()
    median_spacing_mm: float | None = None
    k_fit: float = 3.0
    mm_per_unit: float = 1.0
    outliers: tuple = ()
    #: The label of every correspondence ("A-1"), in the same order as
    #: :attr:`points` — so a finding can say which grid bay it is in.
    labels: tuple = ()

    # ── construction ────────────────────────────────────────────────
    @classmethod
    def from_transform_json(cls, t, *, mm_per_unit: float = 1.0,
                            k_fit: float = 3.0) -> "FitContext":
        """Build a context from ``transform.json`` (a dict, a path or ``None``).

        Accepts anything :func:`core.transform2d.Affine2D.from_json` accepts
        plus a :class:`FitContext` (returned with its scale re-applied), so a
        caller never has to know which one it is holding.
        """
        if isinstance(t, FitContext):
            return cls(**{**t.__dict__, "mm_per_unit": float(mm_per_unit) or 1.0})
        if isinstance(t, (str, Path)):
            t = json.loads(Path(t).read_text(encoding="utf-8"))
        if t is None:
            return cls(mm_per_unit=float(mm_per_unit) or 1.0, k_fit=float(k_fit))
        if not isinstance(t, dict):
            t = getattr(t, "__dict__", None) or {}
        mm = float(mm_per_unit) or 1.0

        pts, loo, labels = [], [], []
        for c in t.get("correspondences") or ():
            if not isinstance(c, dict) or not c.get("inlier", True):
                continue
            m = c.get("mech")
            if not m or len(m) < 2:
                continue
            r = c.get("loo_residual")
            if r is None:
                r = c.get("residual")
            if r is None:
                continue
            pts.append((float(m[0]), float(m[1])))
            loo.append(abs(float(r)) * mm)          # already MECH drawing units
            labels.append(str(c.get("label") or ""))
        quality = t.get("quality") or {}
        return cls(
            points=np.asarray(pts, dtype=float).reshape(-1, 2),
            loo=np.asarray(loo, dtype=float),
            rms=abs(float(t.get("rms_residual") or 0.0)) * mm,
            status=str(quality.get("status") or "ok"),
            warnings=tuple(quality.get("warnings") or ()),
            median_spacing_mm=_maybe_float(t.get("median_spacing"), mm),
            k_fit=float(k_fit),
            mm_per_unit=mm,
            outliers=tuple(str(o) for o in (t.get("outliers") or ())),
            labels=tuple(labels),
        )

    # ── the uncertainty field ───────────────────────────────────────
    def sigma_at(self, p) -> float:
        """Uncertainty of the transform at ``p``, in mm.

        ``max(rms, IDW)`` — the global RMS is the floor, so a fit with no
        spread at all still reports its own scale of error, and the local
        interpolation can only ever raise the tolerance above it.
        """
        if len(self.points) == 0:
            return float(self.rms)
        q = np.asarray([float(p[0]), float(p[1])], dtype=float)
        d = np.hypot(self.points[:, 0] - q[0], self.points[:, 1] - q[1])
        order = np.lexsort((np.arange(len(d)), d))[:IDW_K]
        dist = d[order]
        # a query sitting *on* a correspondence has an undefined IDW weight;
        # taking that correspondence's own residual is the honest answer
        floor = max(1e-9, 1e-9 * float(np.abs(self.points).max()))
        near = dist <= floor
        if np.any(near):
            return float(max(self.rms, float(self.loo[order][near].max())))
        w = 1.0 / np.power(dist, IDW_POWER)
        return float(max(self.rms, float((w * self.loo[order]).sum() / w.sum())))

    def tol_eff(self, tol_mm, p) -> float:
        """``hypot(tol, k_fit · sigma_at(p))`` (§3)."""
        return float(math.hypot(float(tol_mm), float(self.k_fit) * self.sigma_at(p)))

    @property
    def weak(self) -> bool:
        """Is the fit good enough to trust the tolerances at all?"""
        return self.status in ("weak", "forced")

    def to_dict(self) -> dict:
        return {
            "n_correspondences": int(len(self.points)),
            "rms_residual_mm": round(float(self.rms), 6),
            "sigma_max_mm": round(max((self.sigma_at(p) for p in self.points), default=0.0), 6),
            "status": self.status,
            "warnings": list(self.warnings),
            "k_fit": float(self.k_fit),
            "mm_per_unit": float(self.mm_per_unit),
            "median_spacing_mm": (None if self.median_spacing_mm is None
                                  else round(float(self.median_spacing_mm), 6)),
            "outliers": list(self.outliers),
        }


def _maybe_float(v, mm):
    """``transform.json``'s ``median_spacing`` (MECH units) in mm, or ``None``."""
    return None if v is None else abs(float(v)) * float(mm or 1.0)


# ── the displacement analysis ───────────────────────────────────────────────

@dataclass
class Displacement:
    """One finding that carries a direction, and the direction it carries.

    ``normal`` is the MECH-world unit vector of the member normal for support
    findings — §3 says only that component of a support's offset is a real
    displacement, because sliding a saddle *along* a beam is not a defect.
    """

    finding: Finding
    xy: tuple                      # MECH world, drawing units
    offset: tuple                  # mm, (STR − MECH)
    tol_mm: float
    normal: tuple | None = None


def _point_index(points) -> GridIndex:
    """A :class:`~core.spatial.GridIndex` over bare points.

    ``GridIndex`` works on boxes, so a point becomes a zero-area box — the
    neighbour queries in §3 are point queries, and a zero-area box still lands
    in exactly the cells its point is in.
    """
    p = np.asarray(points, dtype=float).reshape(-1, 2)
    return GridIndex(np.hstack([p, p]), cell=None) if len(p) else GridIndex(np.empty((0, 4)))


def _median2(vals) -> tuple[float, float]:
    """Component-wise median of ``[(x, y), …]``.

    On an even count this takes the upper of the two middles, so the answer is
    always a value that actually occurred and the function never invents a
    half-step that no item could have produced.
    """
    if not vals:
        return (0.0, 0.0)
    xs = sorted(float(v[0]) for v in vals)
    ys = sorted(float(v[1]) for v in vals)
    n = len(xs)
    return (xs[n // 2], ys[n // 2])


def analyse(env, findings: list[Finding], fit: FitContext) -> list[Finding]:
    """Apply §3 to the findings and return the resulting list (same object).

    ``findings`` is the whole list; only the displacement kinds get a
    neighbour median subtracted.  Each comes back with ``local_offset_mm``,
    ``local_magnitude_mm``, ``tolerance_eff_mm`` and ``ratio``, plus
    ``systematic`` where the neighbourhood explains the offset away.  A
    systematic item is demoted to INFO or dropped according to
    ``context.emit_systematic_items``, and every surviving cluster becomes one
    ``SYSTEMATIC_OFFSET``.  The list is mutated in place and returned.
    """
    pol = env.policy
    disp = _displacements(env, findings)
    if disp:
        index = _point_index([d.xy for d in disp])
        radius_u = env.u(pol.context.radius_mm)
        k = int(pol.context.k_neighbours)
        global_median = _guarded_global_median(env, disp, fit)
        for i, d in enumerate(disp):
            d.neighbours = [int(v) for v in index.knn(d.xy, k + 1, radius_u)
                            if int(v) != i]
            m = (_median2([disp[j].offset for j in d.neighbours])
                 if len(d.neighbours) >= int(pol.context.min_neighbours)
                 else global_median)
            if d.normal is not None:
                dot = m[0] * d.normal[0] + m[1] * d.normal[1]
                m = (dot * d.normal[0], dot * d.normal[1])
            f = d.finding
            local = (d.offset[0] - m[0], d.offset[1] - m[1])
            lm = math.hypot(local[0], local[1])
            tol_eff = fit.tol_eff(d.tol_mm, d.xy)
            f.local_offset_mm = local
            f.local_magnitude_mm = lm
            f.tolerance_eff_mm = tol_eff
            f.ratio = (lm / tol_eff) if tol_eff > 0 else None
            f.details.setdefault("neighbour_median_mm",
                                 (round(m[0], 4), round(m[1], 4)))
            if lm > tol_eff:
                continue                                  # a local defect
            if lm > d.tol_mm:
                f.severity = "INFO"                       # inside the fit's error
                f.within_fit_uncertainty = True
            elif math.hypot(*d.offset) > d.tol_mm:
                f.systematic = True                       # the neighbourhood moved too
                if pol.context.emit_systematic_items == "info":
                    f.severity = "INFO"
                else:
                    f.details["_dropped"] = True
        findings[:] = [f for f in findings if not f.details.pop("_dropped", False)]
        findings.extend(_systematic(env, disp, fit))

    _tolerances_only(findings, fit)
    if fit.weak:
        for f in findings:
            f.confidence = "low"
    return findings


def _tolerances_only(findings, fit: FitContext) -> None:
    """Every finding still gets a widened tolerance, displacement or not."""
    for f in findings:
        if f.kind in DISPLACEMENT_KINDS:
            continue
        tol_eff = fit.tol_eff(float(f.tolerance_mm), f.location)
        f.tolerance_eff_mm = tol_eff
        f.ratio = f.magnitude() / tol_eff if tol_eff > 0 else None


def _displacements(env, findings) -> list[Displacement]:
    """Every finding whose ``offset_mm`` is a displacement (§3)."""
    out: list[Displacement] = []
    for f in findings:
        if f.kind not in DISPLACEMENT_KINDS or f.offset_mm is None:
            continue
        normal = _band_normal(env, f) if f.kind.startswith("SUPPORT") else None
        out.append(Displacement(
            finding=f,
            xy=(float(f.location[0]), float(f.location[1])),
            offset=(float(f.offset_mm[0]), float(f.offset_mm[1])),
            tol_mm=float(f.tolerance_mm), normal=normal))
    return out


def _band_normal(env, f) -> tuple | None:
    """The unit normal of the member a support finding was measured against."""
    ref = f.str_ref
    if ref is None:
        return None
    for b in env.str_by_family.get(ref.family, ()):
        if b.handle != ref.handle:
            continue
        L = math.hypot(float(b.ux), float(b.uy))
        return None if L <= 0.0 else (-float(b.uy) / L, float(b.ux) / L)
    return None


def _guarded_global_median(env, disp, fit) -> tuple[float, float]:
    """The global median — used only when the drawing really is shifted.

    See the module docstring: the fallback exists for a drawing where every
    item moved, and applying it to a drawing where they did not is how a
    single real defect gets median-subtracted into silence.
    """
    pol = env.policy
    if len(disp) < int(pol.context.min_neighbours):
        return (0.0, 0.0)
    g = _median2([d.offset for d in disp])
    agree = sum(1 for d in disp
                if math.hypot(d.offset[0] - g[0], d.offset[1] - g[1])
                <= fit.tol_eff(d.tol_mm, d.xy))
    if agree < float(pol.context.systematic_min_fraction) * len(disp):
        return (0.0, 0.0)
    return g


def _systematic(env, disp, fit) -> list[Finding]:
    """One ``SYSTEMATIC_OFFSET`` per coherent cluster (§3)."""
    pol = env.policy
    items = [d for d in disp if d.finding.systematic]
    if not items:
        return []
    index = _point_index([d.xy for d in items])
    radius_u = env.u(pol.context.radius_mm)
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in index.knn(items[i].xy, len(items), radius_u):
            j = int(j)
            if j > i and find(i) != find(j):
                parent[max(find(i), find(j))] = min(find(i), find(j))

    clusters: dict[int, list[int]] = {}
    for i in range(len(items)):
        clusters.setdefault(find(i), []).append(i)

    out: list[Finding] = []
    need = int(pol.context.systematic_min_items)
    frac = float(pol.context.systematic_min_fraction)
    for members in clusters.values():
        members.sort(key=lambda i: (items[i].xy, i))
        if len(members) < need:
            continue
        pts = [items[i].xy for i in members]
        lo = (min(p[0] for p in pts), min(p[1] for p in pts))
        hi = (max(p[0] for p in pts), max(p[1] for p in pts))
        is_global = math.hypot(hi[0] - lo[0], hi[1] - lo[1]) > radius_u
        if is_global and len(members) < frac * len(disp):
            continue
        med = _median2([items[i].offset for i in members])
        mag = math.hypot(*med)
        tol = max(items[i].tol_mm for i in members)
        near = max(fit.sigma_at(p) for p in pts)
        reason = (SYSTEMATIC_REASON_LOCAL if near <= tol
                  else SYSTEMATIC_REASON_FIT).format(magnitude=mag)
        out.append(Finding(
            kind="SYSTEMATIC_OFFSET", severity="WARNING",
            location=(float(np.mean([p[0] for p in pts])),
                      float(np.mean([p[1] for p in pts]))),
            offset_mm=med, magnitude_mm=mag, tolerance_mm=tol,
            details={"n_items": len(members),
                     "bbox": [lo[0], lo[1], hi[0], hi[1]],
                     "global": bool(is_global), "sigma_near_mm": near},
            message=format_message("SYSTEMATIC_OFFSET", n_items=len(members),
                                   magnitude=mag, reason=reason)))
    return out
