#!/usr/bin/env python3
"""core/transform2d.py — 2-D similarity transforms, robust fitting and RANSAC.

This is PLAN.md step 5's geometry kernel.  It knows nothing about DXFs,
grids or jobs; it only maps point sets.

Conventions
-----------
An :class:`Affine2D` stores the six numbers of

    x' = a·x + b·y + tx
    y' = c·x + d·y + ty

i.e. the *column-vector* convention used everywhere else in the app (and by
``tests/synth.GroundTruth.as_affine``).  The constructor takes them in that
order, so ``Affine2D(*truth.as_affine())`` is the ground-truth transform.

Public API
----------
``Affine2D(a, b, c, d, tx, ty)``
    ``apply`` / ``apply_many`` / ``inverse`` / ``compose`` / ``to_matrix44``
    / ``decompose`` / ``to_json`` / ``from_json``.
``fit_similarity(src, dst, allow_mirror=False)`` — Umeyama closed form.
``fit_affine(src, dst)`` — unconstrained least squares (diagnostic only).
``ransac_similarity(src, dst, tau, allow_mirror=False)`` — deterministic,
    enumerates every pair of correspondences as a minimal set.
``loo_residuals(src, dst, inlier_idx)`` — leave-one-out residuals.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

import numpy as np
from ezdxf.math import Matrix44

__all__ = [
    "Affine2D",
    "TransformError",
    "FitError",
    "RansacResult",
    "fit_similarity",
    "fit_affine",
    "ransac_similarity",
    "loo_residuals",
    "residuals",
    "rms",
    "point_pair_similarity",
]


class TransformError(RuntimeError):
    """A transform could not be solved from the data given."""


# `core.fit` re-exports FitError from here so a caller only needs one import.
FitError = TransformError


def _as_points(pts, name: str) -> np.ndarray:
    """Coerce to an (N, 2) float array; raise TransformError on anything else."""
    a = np.asarray(pts, dtype=float)
    if a.ndim == 1:
        a = a.reshape(1, -1)
    if a.ndim != 2 or a.shape[1] < 2:
        raise TransformError(f"{name} must be an (N, 2) array of points, got shape {a.shape}")
    a = a[:, :2].copy()
    if not np.all(np.isfinite(a)):
        raise TransformError(f"{name} contains non-finite coordinates")
    return a


@dataclass(frozen=True)
class Affine2D:
    """``x' = a·x + b·y + tx ; y' = c·x + d·y + ty``."""

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    tx: float = 0.0
    ty: float = 0.0

    # ── basic algebra ────────────────────────────────────────────────
    @property
    def det(self) -> float:
        return self.a * self.d - self.b * self.c

    def apply(self, p) -> tuple[float, float]:
        """Map one point (any 2-sequence) -> ``(x, y)``."""
        x = float(p[0])
        y = float(p[1])
        return (self.a * x + self.b * y + self.tx,
                self.c * x + self.d * y + self.ty)

    def apply_many(self, pts) -> np.ndarray:
        """Map an ``(N, 2)`` array -> ``(N, 2)`` float array."""
        a = _as_points(pts, "points")
        out = np.empty_like(a)
        out[:, 0] = self.a * a[:, 0] + self.b * a[:, 1] + self.tx
        out[:, 1] = self.c * a[:, 0] + self.d * a[:, 1] + self.ty
        return out

    def inverse(self) -> "Affine2D":
        """The transform ``p ↦ self⁻¹(p)``. Raises on a singular matrix."""
        det = self.det
        if abs(det) < 1e-15:
            raise TransformError(
                f"transform is singular (det={det:.3e}); it cannot be inverted")
        ia, ib = self.d / det, -self.b / det
        ic, id_ = -self.c / det, self.a / det
        return Affine2D(ia, ib, ic, id_,
                        -(ia * self.tx + ib * self.ty),
                        -(ic * self.tx + id_ * self.ty))

    def compose(self, other: "Affine2D") -> "Affine2D":
        """``self ∘ other`` — ``other`` is applied first, then ``self``."""
        o = _as_affine(other)
        return Affine2D(
            self.a * o.a + self.b * o.c,
            self.a * o.b + self.b * o.d,
            self.c * o.a + self.d * o.c,
            self.c * o.b + self.d * o.d,
            self.a * o.tx + self.b * o.ty + self.tx,
            self.c * o.tx + self.d * o.ty + self.ty,
        )

    def __matmul__(self, other):
        return self.compose(other)

    # ── conversion ───────────────────────────────────────────────────
    def to_matrix44(self) -> Matrix44:
        """An ezdxf ``Matrix44`` for :func:`ezdxf.transform.inplace`.

        ezdxf uses the row-vector convention (``v' = v · M``), so the
        translation lands in the last row and the two coefficients of ``x'``
        in the first column.  ``Matrix44.from_2d_transformation`` expects
        the components as ``(m11, m12, m21, m22, m31, m32)`` — hence the
        ``(a, c, b, d, tx, ty)`` order.  The z row/column is left at the
        identity: this is a 2-D transform, elevations pass through.
        """
        return Matrix44.from_2d_transformation((self.a, self.c, self.b, self.d,
                                                self.tx, self.ty))

    @classmethod
    def from_matrix44(cls, m: Matrix44) -> "Affine2D":
        """Inverse of :meth:`to_matrix44`.

        ``get_2d_transformation()`` is row-major ``[m11, m12, 0, m21, m22, 0,
        m31, m32, 1]``, so the first row carries ``(a, c)`` and the second
        ``(b, d)``.
        """
        g = m.get_2d_transformation()
        return cls(float(g[0]), float(g[3]), float(g[1]), float(g[4]),
                   float(g[6]), float(g[7]))

    def to_matrix(self) -> list:
        """``[[a, b, tx], [c, d, ty]]`` — the ``matrix`` key of transform.json."""
        return [[self.a, self.b, self.tx], [self.c, self.d, self.ty]]

    def decompose(self) -> tuple[float, float, bool, float]:
        """``(scale, rotation_deg, mirrored, skew_deg)``.

        * ``scale`` is the mean of the two column norms (both are equal for a
          true similarity).
        * ``rotation_deg`` is the direction of the first column, in ``(-180, 180]``.
        * ``mirrored`` is ``det < 0``.
        * ``skew_deg`` is ``0`` when the two columns are perpendicular (every
          similarity, mirrored or not) and grows towards ±90 as they approach
          being parallel. It is *not* an anisotropy measure — use
          :meth:`column_scales` for that.
        """
        s1 = math.hypot(self.a, self.c)
        s2 = math.hypot(self.b, self.d)
        scale = 0.5 * (s1 + s2)
        rot = math.degrees(math.atan2(self.c, self.a))
        mirrored = bool(self.det < 0.0)
        if s1 > 1e-15 and s2 > 1e-15:
            cosang = (self.a * self.b + self.c * self.d) / (s1 * s2)
            skew = math.degrees(math.asin(max(-1.0, min(1.0, cosang))))
        else:
            skew = float("nan")
        return scale, rot, mirrored, skew

    def column_scales(self) -> tuple[float, float]:
        """``(x_scale, y_scale)`` — the two column norms."""
        return math.hypot(self.a, self.c), math.hypot(self.b, self.d)

    def is_similarity(self, tol: float = 1e-9) -> bool:
        """True when both columns have the same length and are perpendicular."""
        s1, s2 = self.column_scales()
        if s1 <= 1e-15:
            return False
        return abs(s1 - s2) <= tol * max(s1, s2) and abs(self.a * self.b + self.c * self.d) <= tol * s1 * s2

    # ── serialisation ────────────────────────────────────────────────
    def to_json(self) -> dict:
        scale, rot, mirrored, skew = self.decompose()
        return {
            "matrix": self.to_matrix(),
            "a": self.a, "b": self.b, "c": self.c, "d": self.d,
            "tx": self.tx, "ty": self.ty,
            "scale": scale, "rotation_deg": rot,
            "mirrored": bool(mirrored), "skew_deg": skew,
        }

    @classmethod
    def from_json(cls, d) -> "Affine2D":
        """Build from a ``transform.json``.

        Accepts, in order of preference: the ``matrix`` key, the flat
        ``{a, b, c, d, tx, ty}`` keys, and the **legacy**
        ``{scale_x, scale_y, tx, ty}`` shape (which is a diagonal-only
        affine, so ``b = c = 0``).
        """
        if isinstance(d, str):
            d = json.loads(d)
        if isinstance(d, Affine2D):
            return d
        if not isinstance(d, dict):
            raise TransformError(f"cannot build an Affine2D from {type(d).__name__}")
        if d.get("matrix"):
            m = d["matrix"]                    # [[a, b, tx], [c, d, ty]]
            return cls(float(m[0][0]), float(m[0][1]), float(m[1][0]),
                       float(m[1][1]), float(m[0][2]), float(m[1][2]))
        if "a" in d:
            return cls(float(d["a"]), float(d.get("b", 0.0)), float(d.get("c", 0.0)),
                       float(d["d"]), float(d.get("tx", 0.0)), float(d.get("ty", 0.0)))
        if "scale_x" in d or "scale_y" in d:
            return cls(float(d.get("scale_x", 1.0)), 0.0, 0.0,
                       float(d.get("scale_y", 1.0)),
                       float(d.get("tx", 0.0)), float(d.get("ty", 0.0)))
        raise TransformError("no matrix / a / scale_x in the transform JSON")


def _as_affine(x) -> Affine2D:
    if isinstance(x, Affine2D):
        return x
    return Affine2D.from_json(x)


# ─────────────────────────────────────────────────────────────────────────────
# Least squares
# ─────────────────────────────────────────────────────────────────────────────

def residuals(affine, src, dst) -> np.ndarray:
    """Euclidean distance between ``dst`` and ``affine(src)``, per point."""
    a = _as_affine(affine)
    src = _as_points(src, "src")
    dst = _as_points(dst, "dst")
    if src.shape != dst.shape:
        raise TransformError(f"src {src.shape} and dst {dst.shape} must have the same shape")
    diff = a.apply_many(src) - dst
    return np.hypot(diff[:, 0], diff[:, 1])


def rms(values) -> float:
    """Root-mean-square of an iterable of numbers (0 for an empty one)."""
    v = np.asarray(values, dtype=float)
    if v.size == 0:
        return 0.0
    return float(math.sqrt(float(np.mean(v ** 2))))


def fit_similarity(src, dst, allow_mirror: bool = False) -> Affine2D:
    """Least-squares similarity (uniform scale + rotation + translation).

    Umeyama (1991) closed form.  ``allow_mirror=False`` (the default, and
    what the pipeline uses) always returns a rotation-preserving fit.
    """
    s = _as_points(src, "src")
    t = _as_points(dst, "dst")
    if s.shape != t.shape:
        raise TransformError(f"src {s.shape} and dst {t.shape} must have the same shape")
    n = s.shape[0]
    if n < 2:
        raise TransformError(f"a similarity needs at least 2 correspondences, got {n}")

    mu_s = s.mean(axis=0)
    mu_t = t.mean(axis=0)
    sc = s - mu_s
    tc = t - mu_t
    var_s = float((sc ** 2).sum()) / n
    if var_s <= 0.0:
        raise TransformError("all source points coincide; the scale is undefined")

    cov = (tc.T @ sc) / n                    # 2x2
    U, D, Vt = np.linalg.svd(cov)
    S = np.eye(2)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0.0 and not allow_mirror:
        # The data is mirrored: flip the smallest singular direction so R is a
        # rotation.  With SVD singular values sorted descending this also keeps
        # trace(D·S) ≥ 0, i.e. the scale comes out positive.
        S[1, 1] = -1.0
    R = U @ S @ Vt
    scale = float((D * np.diag(S)).sum()) / var_s
    tvec = mu_t - scale * (R @ mu_s)
    return Affine2D(float(scale * R[0, 0]), float(scale * R[0, 1]),
                    float(scale * R[1, 0]), float(scale * R[1, 1]),
                    float(tvec[0]), float(tvec[1]))


def fit_affine(src, dst) -> Affine2D:
    """Unconstrained least-squares affine (6 parameters).

    Diagnostic only: the pipeline fits a similarity, because independent x/y
    scales overfit and are physically meaningless for two drawings of the
    same building.
    """
    s = _as_points(src, "src")
    t = _as_points(dst, "dst")
    if s.shape != t.shape:
        raise TransformError(f"src {s.shape} and dst {t.shape} must have the same shape")
    n = s.shape[0]
    if n < 3:
        raise TransformError(f"an affine needs at least 3 correspondences, got {n}")
    design = np.column_stack([s[:, 0], s[:, 1], np.ones(n)])
    try:
        coef, *_ = np.linalg.lstsq(design, t, rcond=None)
    except np.linalg.LinAlgError as exc:            # pragma: no cover - rare
        raise TransformError(f"affine least squares failed: {exc}") from exc
    return Affine2D(float(coef[0, 0]), float(coef[0, 1]),
                    float(coef[1, 0]), float(coef[1, 1]),
                    float(coef[2, 0]), float(coef[2, 1]))


def point_pair_similarity(p0, q0, p1, q1, allow_mirror: bool = False) -> Affine2D | None:
    """The exact similarity taking ``p0→q0`` and ``p1→q1`` (or ``None``).

    Two non-coincident point correspondences determine a similarity
    completely.  Returns ``None`` when the two source points coincide.
    """
    p0 = np.asarray(p0, dtype=float)[:2]
    p1 = np.asarray(p1, dtype=float)[:2]
    q0 = np.asarray(q0, dtype=float)[:2]
    q1 = np.asarray(q1, dtype=float)[:2]
    sp = p1 - p0
    sq = q1 - q0
    sp_len = float(math.hypot(sp[0], sp[1]))
    sq_len = float(math.hypot(sq[0], sq[1]))
    if sp_len < 1e-12 or sq_len < 1e-12:
        return None
    # scale·cosθ = |sq|/|sp| · (sp·sq)/(|sp||sq|) = (sp·sq)/|sp|², and likewise
    # for sinθ — the |sp||sq| denominators cancel the scale exactly.
    dot = float(sp[0] * sq[0] + sp[1] * sq[1]) / (sp_len * sp_len)
    cross = float(sp[0] * sq[1] - sp[1] * sq[0]) / (sp_len * sp_len)
    a, b = dot, -cross                     # [[dot, -cross], [cross, dot]] · scale
    c, d = -b, a
    if allow_mirror:
        c, d = b, -a                       # reflect about the x axis first
    return Affine2D(a, b, c, d,
                    float(q0[0] - a * p0[0] - b * p0[1]),
                    float(q0[1] - c * p0[0] - d * p0[1]))


# ─────────────────────────────────────────────────────────────────────────────
# RANSAC
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RansacResult:
    transform: Affine2D
    residuals: np.ndarray          # residual of every correspondence (float)
    inliers: np.ndarray            # bool mask over the correspondences
    indices: np.ndarray            # indices of the inliers, ascending
    tau: float
    rms: float
    n_candidates: int = 0         # how many minimal sets were evaluated
    refit: bool = False

    @property
    def n_inliers(self) -> int:
        return int(self.inliers.sum())

    @property
    def n_total(self) -> int:
        return int(self.inliers.size)

    @property
    def outlier_indices(self) -> list:
        return [int(i) for i in np.flatnonzero(~self.inliers)]


def ransac_similarity(src, dst, tau: float, allow_mirror: bool = False,
                     max_points: int = 400) -> RansacResult:
    """Deterministic RANSAC over all pairs of correspondences.

    Every unordered pair ``(i, j)`` with ``i < j`` and two *distinct* source
    points is a minimal set; the similarity it implies is scored by the
    number of points within ``tau`` and, on ties, by the inlier RMS.  With
    N ≤ ~100 correspondences that is at most 4,950 candidates, so the result
    is reproducible (no RNG anywhere).  The winner is refit on its inliers
    and re-scored once.

    ``max_points`` caps the O(N²) enumeration: above it, only the first
    ``max_points`` correspondences take part (the grid has tens, not
    thousands).
    """
    s = _as_points(src, "src")
    t = _as_points(dst, "dst")
    if s.shape != t.shape:
        raise TransformError(f"src {s.shape} and dst {t.shape} must have the same shape")
    n = s.shape[0]
    if n < 2:
        raise TransformError(f"RANSAC needs at least 2 correspondences, got {n}")
    tau = float(tau)
    if not (tau > 0.0):
        raise TransformError(f"tau must be > 0, got {tau}")

    s_use, t_use = s[:max_points], t[:max_points]
    n_use = s_use.shape[0]

    best: tuple | None = None          # (-n_inliers, rms, index, affine, res)
    candidates = 0
    for i in range(n_use - 1):
        for j in range(i + 1, n_use):
            cand = point_pair_similarity(s_use[i], t_use[i], s_use[j], t_use[j],
                                         allow_mirror=allow_mirror)
            if cand is None:
                continue
            candidates += 1
            res = residuals(cand, s_use, t_use)
            mask = res <= tau
            k = int(mask.sum())
            if k < 2:
                continue
            r = rms(res[mask])
            key = (-k, r)
            if best is None or key < best[0]:
                best = (key, i, j, cand, res, mask)

    if best is None:
        raise TransformError(
            f"no consistent 2-point hypothesis among {candidates} candidate sets "
            f"(tau={tau:.6g}); the correspondences may be unorderable")

    _, _, _, cand, res, mask = best
    affine = cand
    refit = False
    if mask.sum() >= 3:                      # 2 points fit exactly; refit is a no-op
        try:
            trial = fit_similarity(s_use[mask], t_use[mask], allow_mirror=allow_mirror)
        except TransformError:
            trial = None
        if trial is not None:
            tres = residuals(trial, s_use, t_use)
            tmask = tres <= tau
            if int(tmask.sum()) >= int(mask.sum()):
                affine, res, mask, refit = trial, tres, tmask, True

    # Score every correspondence, not just the ones the search looked at.
    full_res = residuals(affine, s, t)
    full_mask = full_res <= tau
    return RansacResult(
        transform=affine,
        residuals=full_res,
        inliers=full_mask,
        indices=np.flatnonzero(full_mask),
        tau=tau,
        rms=rms(full_res[full_mask]),
        n_candidates=candidates,
        refit=refit,
    )


def loo_residuals(src, dst, indices) -> np.ndarray:
    """Leave-one-out residual at each index in ``indices``.

    ``indices`` is the **inlier set** of the fit being audited: for every
    ``i`` in it the transform is refit on the remaining inliers (``indices
    \\ {i}``, *not* the full data set — outliers would otherwise drag the
    refit back towards themselves) and evaluated at ``i``.

    Needs at least 3 inliers: 2 points fit exactly, so the LOO residual
    would be a meaningless 0.  Raises otherwise.
    """
    s = _as_points(src, "src")
    t = _as_points(dst, "dst")
    if s.shape != t.shape:
        raise TransformError(f"src {s.shape} and dst {t.shape} must have the same shape")
    idx = np.asarray(list(indices), dtype=int)
    if idx.size < 3:
        raise TransformError(
            f"leave-one-out needs at least 3 inliers, got {idx.size}")
    if idx.min() < 0 or idx.max() >= s.shape[0]:
        raise TransformError(f"loo indices {idx.tolist()} out of range for {s.shape[0]} points")
    out = np.empty(idx.size, dtype=float)
    for k, i in enumerate(idx):
        rest = np.delete(idx, k)
        try:
            m = fit_similarity(s[rest], t[rest])
        except TransformError:
            m = fit_affine(s[rest], t[rest])
        out[k] = residuals(m, s[i:i + 1], t[i:i + 1])[0]
    return out