#!/usr/bin/env python3
"""core/fit.py — Stage 6: robust STR → MECH transform.

Plan: PLAN.md step 5.  The old separable 1-D least squares (one slope per
axis, no rotation, no outlier rejection, a silent ``slope = 1`` fallback
when the axis coordinates were degenerate) is gone.  What happens now:

1. ``core.grid`` detects the gridlines of both drawings in *world*
   coordinates (step 4) — block base points, rotation, mirroring and
   nesting already folded in.
2. The shared gridline **intersections** are used as correspondences.  That
   is rotation invariant and drops the old "letters are X, digits are Y"
   assumption.
3. A 2-D **similarity** (uniform scale + rotation + translation) is solved
   with Umeyama, wrapped in a deterministic **RANSAC** over every pair of
   correspondences, and scored with **leave-one-out** residuals.
4. Quality gates refuse to write a bad transform: too few points, too few
   inliers, a poor LOO residual, or a scale that contradicts the two
   ``$INSUNITS`` headers.  ``force`` turns every gate into a warning.

Outputs (under the pair root, ``jobs/<id>/`` for the legacy single-pair
layout):

    transform.json    the superset described below (legacy keys included)
    grid_mech.json    MECH gridlines: point, direction, length, status
    grid_str.json     STR gridlines, same shape

``transform.json`` keys — superset, back-compatible:

    model            "similarity" | "affine"
    matrix           [[a, b, tx], [c, d, ty]]
    a b c d tx ty    the same six numbers, flat
    scale            uniform scale (mean column norm)
    rotation_deg     rotation of the first column, in (-180, 180]
    mirrored         det < 0
    skew_deg         0 for a similarity; grows to ±90 for a shear
    correspondences  [{label, str, mech, predicted, residual,
                      loo_residual, inlier}]
    rms_residual     over the inliers        max_residual   over the inliers
    loo_max          worst leave-one-out residual (null when not computable)
    n_inliers n_total outliers[label]
    units            {mech, str, expected_scale}
    quality          {status, warnings}      status: ok | forced
    tau              the RANSAC inlier radius actually used
    median_spacing   median adjacent-axis distance in the MECH drawing
    convention skipped_axes warnings        (legacy)
    scale_x scale_y tx ty x_labels y_labels intersections   (legacy, exact)

``scale_x`` / ``scale_y`` are the **matrix diagonal** (``a`` and ``d``), *not*
the column norms: for a rotated similarity they are ``scale·cos(rotation_deg)``,
not ``scale``.  They are only meaningful as an axis-aligned model when
``rotation_deg`` is 0; use ``scale`` / ``rotation_deg`` (or the added
``column_scales`` key) for anything else.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields
from pathlib import Path

import numpy as np

from . import grid
from .dxfio import read_internal as _read_internal
from .transform2d import (Affine2D, FitError, TransformError, fit_affine,
                          loo_residuals, ransac_similarity, rms)

from jobs import job_dir, load_job


# ─────────────────────────────────────────────────────────────────────────────
# Options
# ─────────────────────────────────────────────────────────────────────────────

# `job.options` is written by the API (step 9).  Both the field name and the
# API name are accepted for every option; anything unknown is ignored.
_OPTION_ALIASES = {
    "model": ("model", "fit_model"),
    "min_inliers": ("min_inliers",),
    "min_inlier_ratio": ("min_inlier_ratio",),
    "loo_tol_rel": ("loo_tol_rel",),
    "scale_prior_tol": ("scale_prior_tol",),
    "ransac_tau_rel": ("ransac_tau_rel",),
    "allow_mirror": ("allow_mirror",),
    "force": ("force", "allow_weak_fit"),
}
_BOOL = {"model": str, "min_inliers": int, "min_inlier_ratio": float,
         "loo_tol_rel": float, "scale_prior_tol": float,
         "ransac_tau_rel": float, "allow_mirror": bool, "force": bool}


@dataclass
class FitOptions:
    """Tunables for the fit and its quality gates."""

    model: str = "similarity"            # "similarity" (default) | "affine" (diagnostic)
    min_inliers: int = 3
    min_inlier_ratio: float = 0.6
    loo_tol_rel: float = 0.005           # × median MECH grid spacing
    scale_prior_tol: float = 0.02        # |scale/expected − 1|
    ransac_tau_rel: float = 0.002        # × median MECH grid spacing
    allow_mirror: bool = False
    force: bool = False                  # every gate becomes a warning

    @classmethod
    def from_job(cls, job) -> "FitOptions":
        """Defaults, overridden by ``job.options`` (absent on old jobs)."""
        raw = getattr(job, "options", None) or {}
        if not isinstance(raw, dict):
            return cls()
        kw = {}
        for f in fields(cls):
            for key in _OPTION_ALIASES[f.name]:
                if key not in raw:
                    continue
                val = raw[key]
                try:
                    if _BOOL[f.name] is bool:
                        if isinstance(val, str):
                            val = val.strip().lower() in ("1", "true", "yes", "on")
                        kw[f.name] = bool(val)
                    else:
                        kw[f.name] = _BOOL[f.name](val)
                except (TypeError, ValueError):
                    continue            # a bad option value never breaks the run
                break
        return cls(**kw)


def options_for_job(job_id: str) -> FitOptions:
    job = load_job(job_id)
    return FitOptions.from_job(job) if job is not None else FitOptions()


# ─────────────────────────────────────────────────────────────────────────────
# Grid helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ok(res: grid.GridResult) -> dict:
    return {k: a for k, a in res.axes.items() if a.status == "ok" and a.point is not None}


def median_spacing(res: grid.GridResult) -> float:
    """Median distance between adjacent parallel gridlines, in drawing units.

    One median per family (the two grids are orthogonal, so mixing them
    would say nothing useful), then the median of those.  The offset of a
    line is measured *across* its family (along the normal), never along
    the line: every gridline of a family starts at the same corner, so
    their along-line offsets are all equal and say nothing.  Falls back to
    the drawing extent, then to 1.0, so there is always a positive scale
    for the relative thresholds.
    """
    ok = _ok(res)
    per_family = []
    for fam in res.families:
        axes = [ok[k] for k in fam if k in ok]
        if len(axes) < 2:
            continue
        d = axes[0].dir
        nx, ny = -d.y, d.x                     # the family's normal
        base = axes[0].point
        ts = sorted((a.point.x - base.x) * nx + (a.point.y - base.y) * ny
                    for a in axes)
        gaps = [b - a for a, b in zip(ts, ts[1:]) if b - a > 0.0]
        if gaps:
            per_family.append(median(gaps))
    if per_family:
        return median(per_family)
    if res.extent > 0.0:
        return float(res.extent)
    return 1.0


def median(values) -> float:
    v = sorted(float(x) for x in values)
    n = len(v)
    if n == 0:
        raise FitError("median of an empty sequence")
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


def _vertical_family(res: grid.GridResult) -> int:
    """Index (0/1) of the family of *vertical* lines, else 0.

    The legacy ``x_labels``/``y_labels`` keys describe the axis whose
    position is given by its X coordinate (the vertical gridlines) and the
    one given by Y.  That is a property of the line direction, not of the
    label, so it is read from the detected directions.
    """
    ok = _ok(res)
    for i, fam in enumerate(res.families):
        axes = [ok[k] for k in fam if k in ok]
        if not axes:
            continue
        if sum(abs(a.dir.y) > abs(a.dir.x) for a in axes) * 2 > len(axes):
            return i
    return 0


def grid_to_json(res: grid.GridResult, prefix: str) -> dict:
    """The sidecar written as ``grid_mech.json`` / ``grid_str.json``."""
    f0, f1 = res.families
    return {
        "prefix": prefix,
        "suffixes": list(res.axes),
        "families": [list(f0), list(f1)],
        "extent": res.extent,
        "axes": {
            lbl: {
                "label": lbl,
                "status": ax.status,
                "reasons": list(ax.reasons),
                "n_occurrences": ax.n_occ,
                "n_agree": ax.n_agree,
                "point": None if ax.point is None else [ax.point.x, ax.point.y],
                "dir": None if ax.dir is None else [ax.dir.x, ax.dir.y],
                "length": ax.length,
                "midpoint": (None if ax.point is None else
                             [ax.point.x + ax.dir.x * ax.length / 2.0,
                              ax.point.y + ax.dir.y * ax.length / 2.0]),
                "angle_deg": (None if ax.dir is None else
                              math.degrees(math.atan2(ax.dir.y, ax.dir.x)) % 180.0),
            }
            for lbl, ax in res.axes.items()
        },
        "intersections": {f"{a}-{b}": [p.x, p.y]
                          for (a, b), p in sorted(grid.intersections(res).items())},
    }


def _axis_summary(res: grid.GridResult, label: str):
    """``((mid_x, mid_y), [start_x, start_y], angle_deg)`` for one axis.

    ``None`` when the axis was not detected.  The midpoint is what the old
    separable fit reported as an axis coordinate.
    """
    ax = _ok(res).get(label)
    if ax is None:
        return None, None, None
    mid = ax.point + ax.dir * (ax.length / 2.0)
    ang = math.degrees(math.atan2(ax.dir.y, ax.dir.x)) % 180.0
    return (mid.x, mid.y), [ax.point.x, ax.point.y], ang


# ─────────────────────────────────────────────────────────────────────────────
# Units prior
# ─────────────────────────────────────────────────────────────────────────────

def _insunits(doc) -> int:
    try:
        return int(doc.header.get("$INSUNITS", 0) or 0)
    except Exception:
        return 0


def expected_scale(str_units: int, mech_units: int) -> float | None:
    """STR drawing units per MECH unit implied by the two ``$INSUNITS``.

    ``None`` when either header is unitless (0) or the two are not
    convertible (µin / mil / Å …).
    """
    if not str_units or not mech_units:
        return None
    try:
        from ezdxf.units import conversion_factor
        f = float(conversion_factor(str_units, mech_units))
    except Exception:
        return None
    return f if math.isfinite(f) and f > 0.0 else None


# ─────────────────────────────────────────────────────────────────────────────
# The solver — pure geometry + gates, no DXFs
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FitInputs:
    """What the solver needs: shared grid intersections and their scale."""

    labels: list                 # "A-1" … in correspondence order
    src: np.ndarray              # (N, 2) STR coordinates
    dst: np.ndarray              # (N, 2) MECH coordinates
    spacing: float = 1.0         # median MECH grid spacing (the tolerance unit)
    extent: float = 0.0          # MECH drawing extent (tau's absolute floor)
    str_units: int = 0           # $INSUNITS of the STR drawing
    mech_units: int = 0          # $INSUNITS of the MECH drawing


def solve(inp: FitInputs, options: FitOptions | None = None) -> dict:
    """Fit STR → MECH from ``inp`` and return the new-style transform fields.

    Raises :class:`FitError` when a quality gate rejects the fit (unless
    ``options.force``, which downgrades every gate to a warning).  This is
    the whole of stage 6's maths; :func:`run` only gathers the inputs from
    the two drawings and writes the files.
    """
    opts = options or FitOptions()
    src = np.asarray(inp.src, dtype=float)
    dst = np.asarray(inp.dst, dtype=float)
    labels = list(inp.labels)
    if src.shape != dst.shape or src.shape[0] != len(labels):
        raise FitError(
            f"labels ({len(labels)}), src {src.shape} and dst {dst.shape} "
            "must describe the same correspondences")

    warnings: list[str] = []
    status = "ok"

    def gate(ok: bool, msg: str) -> None:
        """Refuse a bad fit — unless ``force`` was asked for."""
        nonlocal status
        if ok:
            return
        if opts.force:
            warnings.append("quality gate overridden by force: " + msg)
            status = "forced"
            return
        raise FitError(msg)

    # ── gate 1: enough shared intersections at all ─────────────────────────
    # Never forceable: with fewer than two points there is nothing to fit.
    if len(labels) < 2:
        raise FitError(
            f"need ≥2 shared grid intersections; found {len(labels)}: "
            f"{', '.join(labels) or '—'}. Check the axes convention, and which "
            "axes failed in grid_mech.json / grid_str.json.")

    spacing = float(inp.spacing) or 1.0
    tau = max(opts.ransac_tau_rel * spacing, 1e-9 * float(inp.extent), 1e-12)

    # ── gate 2: exactly two points is exact but unverifiable ────────────────
    gate(len(labels) > 2,
         f"only {len(labels)} shared grid intersections ({', '.join(labels)}): "
         "the fit is exact but has no redundancy, so it cannot be verified. "
         "Re-run with force=True (allow_weak_fit) to accept it anyway.")

    if opts.model == "affine":
        # Diagnostic model.  The inlier set still comes from the similarity
        # RANSAC — a 3-point minimal set per hypothesis is not worth the
        # combinatorics — then the 6-parameter affine is refit on it and the
        # inliers recomputed from that fit.
        ransac = ransac_similarity(src, dst, tau, allow_mirror=opts.allow_mirror)
        affine, res, mask = ransac.transform, ransac.residuals, ransac.inliers
        if int(mask.sum()) >= 3:
            cand = fit_affine(src[mask], dst[mask])
            cres = np.hypot(*(cand.apply_many(src) - dst).T)
            cmask = cres <= tau
            if int(cmask.sum()) >= 3:
                affine, res, mask = cand, cres, cmask
        model = "affine"
    elif opts.model == "similarity":
        ransac = ransac_similarity(src, dst, tau, allow_mirror=opts.allow_mirror)
        affine, res, mask = ransac.transform, ransac.residuals, ransac.inliers
        model = "similarity"
    else:
        raise FitError(f"unknown fit model {opts.model!r}; "
                       "expected 'similarity' or 'affine'")

    n_total = int(mask.size)
    n_inliers = int(mask.sum())
    outliers = [labels[i] for i in range(n_total) if not mask[i]]

    # ── gate 3: enough inliers ─────────────────────────────────────────────
    ratio = n_inliers / n_total if n_total else 0.0
    gate(n_inliers >= opts.min_inliers and ratio >= opts.min_inlier_ratio,
         f"only {n_inliers}/{n_total} grid intersections agree with the fit "
         f"({ratio:.0%}, need ≥{opts.min_inliers} and ≥{opts.min_inlier_ratio:.0%}). "
         f"Outlier labels: {', '.join(outliers) or '—'}")

    # ── leave-one-out ──────────────────────────────────────────────────────
    inlier_idx = [i for i in range(n_total) if mask[i]]
    loo_by_index: dict[int, float] = {}
    if len(inlier_idx) >= 3:
        try:
            loo = loo_residuals(src, dst, inlier_idx)
        except TransformError as exc:
            # A degenerate refit is a *verification failure*, not a crash:
            # it becomes a gate, so `force` can override it like the others.
            loo_max = None
            gate(False, f"the fit could not be verified by leave-one-out: {exc}")
        else:
            loo_by_index = {i: float(v) for i, v in zip(inlier_idx, loo)}
            loo_max = float(max(loo_by_index.values()))
            # ── gate 4: the fit must not depend on any single point ────────
            gate(loo_max <= opts.loo_tol_rel * spacing,
                 f"leave-one-out residual {loo_max:.4g} exceeds "
                 f"{opts.loo_tol_rel:g} × the median grid spacing ({spacing:.4g}). "
                 "The fit is unstable — check for a mis-paired grid label.")
    else:
        loo_max = None

    # ── gate 5: $INSUNITS scale prior ──────────────────────────────────────
    expected = expected_scale(inp.str_units, inp.mech_units)
    scale, rotation_deg, mirrored, skew_deg = affine.decompose()
    if expected is None:
        warnings.append(
            f"$INSUNITS prior unavailable (MECH={inp.mech_units}, "
            f"STR={inp.str_units}) — the scale was not checked against the "
            "unit headers")
    else:
        rel = abs(scale / expected - 1.0)
        gate(rel <= opts.scale_prior_tol,
             f"$INSUNITS mismatch: $INSUNITS says {inp.str_units} (STR) → "
             f"{inp.mech_units} (MECH), i.e. a scale of {expected:.6g}, but the "
             f"grid intersections give {scale:.6g} ({rel:.1%} off, tolerance "
             f"{opts.scale_prior_tol:.1%}). Either the unit headers are wrong "
             "or the two drawings do not share a grid origin.")

    inlier_res = [res[i] for i in inlier_idx]
    correspondences = []
    for i, lbl in enumerate(labels):
        pred = affine.apply((float(src[i, 0]), float(src[i, 1])))
        correspondences.append({
            "label": lbl,
            "str": [float(src[i, 0]), float(src[i, 1])],
            "mech": [float(dst[i, 0]), float(dst[i, 1])],
            "predicted": [pred[0], pred[1]],
            "residual": float(res[i]),
            "loo_residual": loo_by_index.get(i),
            "inlier": bool(mask[i]),
        })

    return {
        "affine": affine,
        "model": model,
        "matrix": affine.to_matrix(),
        "a": affine.a, "b": affine.b, "c": affine.c, "d": affine.d,
        "tx": affine.tx, "ty": affine.ty,
        "scale": scale,
        "rotation_deg": rotation_deg,
        "mirrored": bool(mirrored),
        "skew_deg": skew_deg,
        "correspondences": correspondences,
        "rms_residual": rms(inlier_res) if inlier_res else 0.0,
        "max_residual": float(max(inlier_res)) if inlier_res else 0.0,
        "loo_max": loo_max,
        "n_inliers": n_inliers,
        "n_total": n_total,
        "outliers": outliers,
        "inlier_ratio": ratio,
        "units": {"mech": inp.mech_units, "str": inp.str_units,
                  "expected_scale": expected},
        "quality": {"status": status, "warnings": list(warnings)},
        "tau": float(tau),
        "median_spacing": float(spacing),
    }


# ─────────────────────────────────────────────────────────────────────────────
# The stage
# ─────────────────────────────────────────────────────────────────────────────

def run(job_id: str, *, pair_dir=None) -> None:
    """Stage 6 entrypoint.

    ``pair_dir`` is the per-pair ``cleaned/`` directory
    (``jobs/<id>/out/pairs/<pair_id>/cleaned/``) for Slice-1 multi-pair
    runs.  When ``None`` (the legacy default) every output is written under
    the job dir instead of the pair root.
    """
    d = job_dir(job_id)
    root = Path(pair_dir).parent if pair_dir is not None else d
    from .roles import resolve_pair
    mech_path, str_path = resolve_pair(job_id, pair_dir)   # explicit roles

    convention_path = root / "axes" / "convention.json"
    if not convention_path.exists():
        raise RuntimeError("axes/convention.json missing — user must confirm axes first")
    convention = json.loads(convention_path.read_text())
    prefix = convention["prefix"]
    axes = list(convention["axes"])
    opts = options_for_job(job_id)

    # World-coordinate grid detection (core/grid.py); MECH = drawing A, STR = B.
    mech_doc = _read_internal(str(mech_path))
    str_doc = _read_internal(str(str_path))
    mech_grid = grid.detect_grid(mech_doc, prefix, axes)
    str_grid = grid.detect_grid(str_doc, prefix, axes)

    # Sidecars for steps 7/8 (QA grid drawing, grid-cropped preview).
    (root / "grid_mech.json").write_text(
        json.dumps(grid_to_json(mech_grid, prefix), indent=2))
    (root / "grid_str.json").write_text(
        json.dumps(grid_to_json(str_grid, prefix), indent=2))

    mech_ix = grid.intersections(mech_grid)
    str_ix = grid.intersections(str_grid)
    keys = sorted(set(mech_ix) & set(str_ix))
    labels = [f"{a}-{b}" for a, b in keys]

    skipped = [
        {"label": lbl,
         "mech": {"status": mech_grid.axes[lbl].status,
                  "reasons": list(mech_grid.axes[lbl].reasons)},
         "str": {"status": str_grid.axes[lbl].status,
                 "reasons": list(str_grid.axes[lbl].reasons)}}
        for lbl in axes
        if lbl in mech_grid.axes and lbl in str_grid.axes
        and (mech_grid.axes[lbl].status != "ok" or str_grid.axes[lbl].status != "ok")
    ]
    extra_warnings = [
        f"axis {lbl} is detected in both drawings but the gridlines never "
        "meet — it contributes no correspondence"
        for lbl in axes
        if mech_grid.axes.get(lbl) is not None and mech_grid.axes[lbl].status == "ok"
        and str_grid.axes.get(lbl) is not None and str_grid.axes[lbl].status == "ok"
        and lbl not in {k[0] for k in keys} and lbl not in {k[1] for k in keys}
    ]

    sol = solve(
        FitInputs(
            labels=labels,
            src=np.array([[str_ix[k].x, str_ix[k].y] for k in keys], dtype=float)
            if keys else np.zeros((0, 2)),
            dst=np.array([[mech_ix[k].x, mech_ix[k].y] for k in keys], dtype=float)
            if keys else np.zeros((0, 2)),
            spacing=median_spacing(mech_grid),
            extent=float(mech_grid.extent) or 0.0,
            str_units=_insunits(str_doc),
            mech_units=_insunits(mech_doc),
        ),
        opts,
    )
    warnings = sol["quality"]["warnings"] + extra_warnings
    affine: Affine2D = sol.pop("affine")

    # Legacy x_labels / y_labels: the vertical and horizontal gridlines with
    # the coordinate of each line's midpoint (what the old separable fit
    # reported).  The extra keys are additive; old readers ignore them.
    #
    # The key is chosen by the line's *direction*, not by its position in
    # `families`: x_labels are the gridlines a reader positions along X (the
    # vertical ones, letters in the classic TP-104 convention), y_labels the
    # horizontal ones.  Pairing family 0 with x_labels instead would silently
    # swap the two for every drawing whose vertical family is not family 0.
    vfam = _vertical_family(mech_grid)
    fam_of = {lbl: fi for fi, fam in enumerate(mech_grid.families) for lbl in fam}
    legacy_axes = {True: [], False: []}        # keyed by "is vertical"
    for fi in (0, 1):
        vertical = (fi == vfam)
        rows = legacy_axes[vertical]
        for lbl in axes:
            if fam_of.get(lbl) != fi:
                continue
            if mech_grid.axes.get(lbl) is None or mech_grid.axes[lbl].status != "ok":
                continue
            if str_grid.axes.get(lbl) is None or str_grid.axes[lbl].status != "ok":
                continue
            s_mid, s_pt, s_ang = _axis_summary(str_grid, lbl)
            m_mid, m_pt, m_ang = _axis_summary(mech_grid, lbl)
            # Vertical lines are positioned by their X coordinate, so the
            # legacy key is the X midpoint; horizontal ones use Y.
            rows.append({"label": lbl,
                         "str": s_mid[0] if vertical else s_mid[1],
                         "mech": m_mid[0] if vertical else m_mid[1],
                         "str_point": s_pt, "mech_point": m_pt,
                         "str_angle_deg": s_ang, "mech_angle_deg": m_ang})
    x_labels, y_labels = legacy_axes[True], legacy_axes[False]

    sol["quality"]["warnings"] = list(warnings)
    transform = {
        **sol,
        # ── legacy, unchanged shape ──
        # `scale_x`/`scale_y` are the matrix diagonal (a, d) — for a rotated
        # similarity that is scale·cos(rotation_deg), NOT the scale.
        "scale_x": affine.a,
        "scale_y": affine.d,
        # the TRUE column norms, added so consumers never have to guess
        "column_scales": list(affine.column_scales()),
        "tx": affine.tx,
        "ty": affine.ty,
        "x_labels": x_labels,
        "y_labels": y_labels,
        "intersections": [
            {"label": c["label"], "str": c["str"], "mech": c["mech"],
             "predicted_mech": c["predicted"], "residual": c["residual"],
             "inlier": c["inlier"]}
            for c in sol["correspondences"]
        ],
        "convention": {"prefix": prefix, "axes": list(axes)},
        "skipped_axes": skipped,
        "warnings": list(warnings),
    }
    (root / "transform.json").write_text(json.dumps(transform, indent=2))


__all__ = ["run", "solve", "FitInputs", "FitOptions", "FitError", "Affine2D",
           "median_spacing", "expected_scale", "grid_to_json", "options_for_job"]