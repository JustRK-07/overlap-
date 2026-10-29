#!/usr/bin/env python3
"""test_fit.py — core/transform2d.py and the core.fit stage (PLAN.md step 5).

Two flavours of test:
  * pure geometry, driven straight through ``core.fit.solve`` /
    ``core.transform2d`` with hand-built correspondences (fast, exact);
  * full-stage, running stages 1-7 of the pipeline on a synthetic pair and
    then ``core.fit.run`` (the integration contract: transform.json shape,
    quality gates, legacy keys).
"""
from __future__ import annotations

import json
import math
import shutil
import sys
import uuid
from pathlib import Path

import ezdxf
import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import synth  # noqa: E402
from core import grid  # noqa: E402
from core.fit import FitError, FitInputs, FitOptions, solve  # noqa: E402
from core.transform2d import (Affine2D, fit_affine, fit_similarity,  # noqa: E402
                              loo_residuals, ransac_similarity)

PREFIX = "TP104-"
AXES = ["A", "B", "C", "D", "1", "2", "3"]
STR_UNITS_MM, STR_UNITS_M = 4, 6      # $INSUNITS codes


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def grid_inputs(pair, *, str_units=None, mech_units=STR_UNITS_MM,
                spacing=None, drop=(), jitter=0.0, seed=3):
    """FitInputs built from the *generator output* of a SynthPair."""
    m = grid.detect_grid(ezdxf.readfile(str(pair.mech_path)), PREFIX, AXES)
    s = grid.detect_grid(ezdxf.readfile(str(pair.str_path)), PREFIX, AXES)
    mi, si = grid.intersections(m), grid.intersections(s)
    keys = [k for k in sorted(set(mi) & set(si)) if f"{k[0]}-{k[1]}" not in drop]
    src = np.array([[si[k].x, si[k].y] for k in keys], dtype=float)
    dst = np.array([[mi[k].x, mi[k].y] for k in keys], dtype=float)
    if jitter:
        dst = dst + np.random.default_rng(seed).normal(0.0, jitter, size=dst.shape)
    if spacing is None:
        from core.fit import median_spacing
        spacing = median_spacing(m)
    if str_units is None:
        str_units = STR_UNITS_M if pair.opts.str_units == "m" else STR_UNITS_MM
    return FitInputs(labels=[f"{a}-{b}" for a, b in keys], src=src, dst=dst,
                     spacing=spacing, extent=m.extent,
                     str_units=str_units, mech_units=mech_units), keys


def fit_a_job(tmp_path, opts, *, axes=None, options=None):
    """Create a job from a synthetic pair and run stages 1-7 (no fit).

    Returns (job_id, pair, job_dir).  ``options`` lands in ``job.options``,
    which is where ``core.fit`` reads its tunables from.
    """
    import pipelines
    from jobs import create_job_legacy, job_dir, load_job, save_job

    pair = synth.build_pair(tmp_path / f"src_{uuid.uuid4().hex[:6]}", opts)
    job = create_job_legacy(
        drawing_a_name=pair.mech_path.name, drawing_a_size=pair.mech_path.stat().st_size,
        drawing_b_name=pair.str_path.name, drawing_b_size=pair.str_path.stat().st_size,
        clean_version="v1",
    )
    up = job_dir(job.job_id) / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pair.mech_path, up / pair.mech_path.name)
    shutil.copyfile(pair.str_path, up / pair.str_path.name)
    pipelines.run_pipeline(job.job_id)
    assert load_job(job.job_id).state == "awaiting_axes"

    d = job_dir(job.job_id)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": opts.prefix, "axes": list(axes or (opts.x_labels + opts.y_labels))}))
    if options is not None:
        j = load_job(job.job_id)
        j.options = dict(options)
        save_job(j)
    return job.job_id, pair, d


@pytest.fixture
def fit_job(fx_isolated_jobs, tmp_path):
    """Factory: ``fit_job(opts=..., axes=..., options=...)`` -> (id, pair, dir)."""
    def _make(opts=None, **kw):
        return fit_a_job(tmp_path, opts or synth.LEGACY_COMPATIBLE, **kw)
    return _make


# ─────────────────────────────────────────────────────────────────────────────
# core.transform2d — the maths
# ─────────────────────────────────────────────────────────────────────────────

def test_umeyama_exact():
    """Umeyama recovers a known similarity to machine-ish precision."""
    truth = synth.GroundTruth(scale=1000.0, rotation_deg=7.5, tx=196034.182, ty=-170819.088)
    want = Affine2D(*truth.as_affine())
    rng = np.random.default_rng(20240917)
    src = rng.uniform(-5e5, 5e5, size=(12, 2))
    dst = truth.apply(src)

    fit = fit_similarity(src, dst)
    # the 2×2 part is scale-free, so it is exact to ~1e-12 relative
    for name, got, exp in (("a", fit.a, want.a), ("b", fit.b, want.b),
                           ("c", fit.c, want.c), ("d", fit.d, want.d)):
        assert got == pytest.approx(exp, rel=1e-12), name
    # the translation inherits the centring round-off of ~1e5-unit coordinates
    assert fit.tx == pytest.approx(want.tx, abs=1e-6)
    assert fit.ty == pytest.approx(want.ty, abs=1e-6)
    scale, rot, mirrored, skew = fit.decompose()
    assert scale == pytest.approx(1000.0, rel=1e-12)
    assert rot == pytest.approx(7.5, abs=1e-9)
    assert mirrored is False
    assert skew == pytest.approx(0.0, abs=1e-9)
    assert fit.is_similarity()


def test_affine2d_inverse_and_matrix44_round_trip():
    truth = synth.GroundTruth(1000.0, 7.5, 196034.182, -170819.088)
    aff = Affine2D(*truth.as_affine())
    inv = aff.inverse()
    for p in [(0.0, 0.0), (1.0, 2.0), (-1.3e5, 9.7e4)]:
        back = inv.apply(aff.apply(p))
        assert back[0] == pytest.approx(p[0], abs=1e-6)
        assert back[1] == pytest.approx(p[1], abs=1e-6)
    assert Affine2D.from_matrix44(aff.to_matrix44()) == aff
    # the ezdxf matrix really maps x' = a·x + b·y + tx
    v = aff.to_matrix44().transform((2.0, 3.0, 0.0))
    assert (v.x, v.y) == pytest.approx(aff.apply((2.0, 3.0)))


def test_affine2d_from_json_accepts_legacy_shape():
    legacy = Affine2D.from_json({"scale_x": 2.0, "scale_y": 3.0, "tx": 1.0, "ty": -1.0})
    assert legacy == Affine2D(2.0, 0.0, 0.0, 3.0, 1.0, -1.0)
    assert legacy.apply((4.0, 5.0)) == (9.0, 14.0)
    # and the superset written by core.fit
    aff = Affine2D(1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    assert Affine2D.from_json(aff.to_json()) == aff


def test_ransac_is_deterministic():
    truth = synth.GroundTruth(1000.0, 7.5, 196034.182, -170819.088)
    aff = Affine2D(*truth.as_affine())
    rng = np.random.default_rng(11)
    src = rng.uniform(-1e5, 1e5, size=(9, 2))
    dst = aff.apply_many(src)
    dst[4] += 900.0                       # one gross outlier
    first = ransac_similarity(src, dst, tau=10.0)
    second = ransac_similarity(src, dst, tau=10.0)
    assert first.transform == second.transform          # no RNG anywhere
    assert first.outlier_indices == second.outlier_indices == [4]
    assert first.n_inliers == 8


def test_fit_affine_is_diagnostic_only():
    """A pure similarity is reproduced exactly by the affine fit too."""
    truth = synth.GroundTruth(7.0, 30.0, 10.0, -4.0)
    src = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [3.0, -2.0]])
    aff = fit_affine(src, truth.apply(src))
    assert aff.decompose()[3] == pytest.approx(0.0, abs=1e-9)
    assert aff.is_similarity()


def test_loo_residuals_need_three_points():
    src = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    with pytest.raises(FitError):
        loo_residuals(src, src + 1.0, [0, 1])


# ─────────────────────────────────────────────────────────────────────────────
# quality gates
# ─────────────────────────────────────────────────────────────────────────────

def test_two_correspondences_weak_requires_force():
    inp = FitInputs(labels=["A-1", "B-2"], src=[[0.0, 0.0], [10.0, 0.0]],
                    dst=[[5.0, 5.0], [15.0, 5.0]], spacing=10.0,
                    extent=100.0, str_units=STR_UNITS_MM, mech_units=STR_UNITS_MM)
    with pytest.raises(FitError) as exc:
        solve(inp, FitOptions())
    assert "only 2 shared grid intersections" in str(exc.value)
    assert "force" in str(exc.value)

    forced = solve(inp, FitOptions(force=True))
    assert forced["quality"]["status"] == "forced"
    assert any("force" in w for w in forced["quality"]["warnings"])
    assert forced["n_inliers"] == 2 and forced["loo_max"] is None


def test_insufficient_raises_fiterror():
    with pytest.raises(FitError) as exc:
        solve(FitInputs(labels=["A-1"], src=[[0.0, 0.0]], dst=[[1.0, 1.0]],
                         spacing=10.0, extent=100.0), FitOptions())
    assert "≥2 shared grid intersections" in str(exc.value)
    assert "found 1" in str(exc.value)

    with pytest.raises(FitError):
        solve(FitInputs(labels=[], src=np.zeros((0, 2)), dst=np.zeros((0, 2)),
                         spacing=10.0, extent=100.0), FitOptions())
    # force cannot conjure a fit out of one point
    with pytest.raises(FitError):
        solve(FitInputs(labels=["A-1"], src=[[0.0, 0.0]], dst=[[1.0, 1.0]],
                         spacing=10.0, extent=100.0), FitOptions(force=True))


def test_loo_positive_with_noise(fx_synth_hard):
    """1 mm of noise: the LOO residual must exceed the plain RMS."""
    inp, _ = grid_inputs(fx_synth_hard, jitter=1.0)
    sol = solve(inp, FitOptions())
    assert 0.0 < sol["rms_residual"] < sol["loo_max"]
    assert sol["loo_max"] == max(c["loo_residual"] for c in sol["correspondences"]
                                 if c["loo_residual"] is not None)
    assert sol["n_inliers"] == sol["n_total"] == len(inp.labels)


def sol_scale(inp):
    """The scale the geometry implies, ignoring the unit headers."""
    return solve(FitInputs(**{**inp.__dict__, "str_units": 0}), FitOptions())["scale"]


def test_units_prior_mismatch_raises(fx_synth_hard):
    """STR geometry is in metres but the header says millimetres."""
    inp, _ = grid_inputs(fx_synth_hard, str_units=STR_UNITS_MM)
    assert sol_scale(inp) == pytest.approx(1000.0)      # the geometry really is m
    with pytest.raises(FitError) as exc:
        solve(inp, FitOptions())
    msg = str(exc.value)
    assert "INSUNITS" in msg and "1000" in msg and "1" in msg

    # and the matching header passes
    ok, _ = grid_inputs(fx_synth_hard, str_units=STR_UNITS_M)
    assert solve(ok, FitOptions())["quality"]["status"] == "ok"


def test_units_prior_absent_is_a_warning_not_an_error():
    inp = FitInputs(labels=[f"A-{i}" for i in range(4)],
                    src=[[0.0, 0.0], [10.0, 0.0], [0.0, 10.0], [10.0, 10.0]],
                    dst=[[1.0, 1.0], [11.0, 1.0], [1.0, 11.0], [11.0, 11.0]],
                    spacing=10.0, extent=100.0, str_units=0, mech_units=0)
    sol = solve(inp, FitOptions())
    assert sol["units"]["expected_scale"] is None
    assert any("INSUNITS" in w for w in sol["quality"]["warnings"])


def test_ransac_rejects_shifted_axis(tmp_path):
    """shift_str_axis={"D": 300}: the D gridline is rejected, not fitted."""
    opts = synth.with_opts(synth.HARD, shift_str_axis={"D": 300.0})
    pair = synth.build_pair(tmp_path / "shifted", opts)
    inp, _ = grid_inputs(pair)
    assert len(inp.labels) == 12

    sol = solve(inp, FitOptions())
    assert sol["outliers"] == ["1-D", "2-D", "3-D"]
    assert sol["quality"]["status"] == "ok"
    assert sol["n_inliers"] == 9
    assert sol["scale"] == pytest.approx(pair.truth.scale, rel=1e-9)
    assert sol["rotation_deg"] == pytest.approx(pair.truth.rotation_deg, abs=1e-9)
    # every inlier is still mapped onto its MECH point
    for c in sol["correspondences"]:
        if not c["inlier"]:
            continue
        assert c["residual"] < 1e-6, c


# ─────────────────────────────────────────────────────────────────────────────
# the stage, end to end
# ─────────────────────────────────────────────────────────────────────────────

def test_fit_stage_hard_pair(fit_job):
    """Rotation 7.5° + metres: previously unrecoverable, now exact."""
    from core import fit as fit_mod
    job_id, pair, d = fit_job(synth.HARD)
    fit_mod.run(job_id)
    t = json.loads((d / "transform.json").read_text())

    assert abs(t["rotation_deg"] - 7.5) < 1e-9, t["rotation_deg"]
    assert abs(t["scale"] - 1000.0) < 1e-6, t["scale"]
    assert t["mirrored"] is False
    assert t["quality"]["status"] == "ok"
    assert t["outliers"] == []
    assert t["units"]["mech"] == STR_UNITS_MM
    assert t["units"]["str"] == STR_UNITS_M
    assert t["units"]["expected_scale"] == 1000.0
    # The units are recorded in full: what the header said, what was
    # chosen, and why.
    assert t["units"]["mech_units"] == "mm"
    assert t["units"]["str_units"] == "m"
    assert t["units"]["mech_header_units"] == "mm"
    assert t["units"]["str_header_units"] == "m"
    assert t["units"]["mech_source"] == "header"
    assert t["units"]["str_source"] == "header"
    assert t["units"]["mech_reason"]
    assert t["units"]["mm_per_unit"] == 1.0

    aff = Affine2D.from_json(t)
    extent = t["median_spacing"] * 4
    checked = 0
    for c in t["correspondences"]:
        a_, b_ = c["label"].split("-")
        # The label is "<family0>-<family1>"; the generator keys its truth
        # by (x_label, y_label), which need not be the same order.
        mpt = pair.intersections_mech.get((a_, b_)) or pair.intersections_mech.get((b_, a_))
        if mpt is None:
            continue
        pred = aff.apply(c["str"])
        assert np.hypot(pred[0] - mpt[0], pred[1] - mpt[1]) <= 1e-6 * extent, c["label"]
        checked += 1
    assert checked >= 4, "no correspondence matched the generator's truth"

    # the grid sidecars step 7/8 need
    gm = json.loads((d / "grid_mech.json").read_text())
    gs = json.loads((d / "grid_str.json").read_text())
    for side in (gm, gs):
        assert set(side["axes"]) == set(AXES)
        assert side["axes"]["A"]["status"] == "ok"
        assert len(side["axes"]["A"]["dir"]) == 2
        assert side["intersections"]


def test_fit_stage_legacy_transform_is_exact(fit_job):
    """The legacy (unrotated, mm) pair still yields the documented numbers."""
    from core import fit as fit_mod
    job_id, pair, d = fit_job(synth.LEGACY_COMPATIBLE)
    fit_mod.run(job_id)
    t = json.loads((d / "transform.json").read_text())
    assert t["tx"] == pytest.approx(196034.182, abs=1e-6)
    assert t["ty"] == pytest.approx(-170819.088, abs=1e-6)
    assert t["scale"] == pytest.approx(1.0, abs=1e-9)
    assert t["rotation_deg"] == pytest.approx(0.0, abs=1e-9)
    assert t["rms_residual"] < 1e-6
    assert t["scale_x"] == pytest.approx(1.0, abs=1e-9)
    assert t["scale_y"] == pytest.approx(1.0, abs=1e-9)


def test_transform_json_legacy_keys_present(fit_job):
    """package.py / app.js / analyze.py keep reading the old shape."""
    from core import fit as fit_mod
    job_id, pair, d = fit_job(synth.HARD)
    fit_mod.run(job_id)
    t = json.loads((d / "transform.json").read_text())

    # legacy keys, exactly as the old stage wrote them
    for k in ("scale_x", "scale_y", "tx", "ty", "x_labels", "y_labels",
              "intersections", "rms_residual", "max_residual"):
        assert k in t, k
    assert t["scale_x"] == t["a"] and t["scale_y"] == t["d"]
    assert t["tx"] == pytest.approx(pair.truth.tx, abs=1e-6)
    assert t["ty"] == pytest.approx(pair.truth.ty, abs=1e-6)
    for row in t["x_labels"] + t["y_labels"]:
        assert set(("label", "str", "mech")) <= set(row)
    assert len(t["x_labels"]) + len(t["y_labels"]) >= 4
    assert t["intersections"], "no legacy intersections"
    for ix in t["intersections"]:
        assert set(("label", "str", "mech", "predicted_mech", "residual")) <= set(ix)
        assert len(ix["str"]) == 2 and len(ix["mech"]) == 2
    # superset keys
    assert Affine2D.from_json(t) == Affine2D(t["a"], t["b"], t["c"], t["d"], t["tx"], t["ty"])
    assert len(t["matrix"]) == 2 and len(t["matrix"][0]) == 3
    assert t["n_total"] == len(t["correspondences"]) == len(t["intersections"])


def test_transform_json_exposes_the_true_column_scales(fit_job):
    """A10: `scale_x`/`scale_y` are the matrix diagonal; `column_scales` is
    the real scale.

    The docstring and the README both called the legacy keys "the column
    norms", but the code writes ``affine.a`` / ``affine.d`` — the diagonal.
    For a rotated similarity those differ by ``cos(rotation_deg)``, so the
    legacy keys under-report the scale by 0.86 % at 7.5 degrees.  The code
    keeps the old contract (the diagonal) and adds the true column norms.
    """
    from core import fit as fit_mod
    job_id, pair, d = fit_job(synth.HARD)
    fit_mod.run(job_id)
    t = json.loads((d / "transform.json").read_text())

    # the legacy keys stay the diagonal — the old contract
    assert t["scale_x"] == t["a"] and t["scale_y"] == t["d"]

    # the new key is the true column norm
    assert t["column_scales"] == pytest.approx([t["scale"], t["scale"]])

    # and for a rotated fit the diagonal is demonstrably NOT the scale
    assert t["rotation_deg"] == pytest.approx(7.5, abs=1e-9)
    assert t["scale_x"] == pytest.approx(
        t["scale"] * math.cos(math.radians(7.5)))
    assert t["scale_x"] != pytest.approx(t["scale"])
    assert t["n_inliers"] + len(t["outliers"]) == t["n_total"]
    assert set(t["quality"]) == {"status", "warnings"}


def test_fit_stage_options_from_job(fit_job):
    """``job.options`` reaches FitOptions through the stage."""
    from core import fit as fit_mod

    # step 9's API name `fit_model` is accepted as well as `model`
    job_id, pair, d = fit_job(synth.HARD, options={"fit_model": "affine"})
    fit_mod.run(job_id)
    t = json.loads((d / "transform.json").read_text())
    assert t["model"] == "affine"
    assert t["quality"]["status"] == "ok"
    assert t["scale"] == pytest.approx(1000.0, rel=1e-6)

    # a model that does not exist is a hard error, not a silent fallback
    job_id, pair, d = fit_job(synth.HARD, options={"model": "nonsense"})
    with pytest.raises(FitError) as exc:
        fit_mod.run(job_id)
    assert "unknown fit model" in str(exc.value)

    # unknown option keys are ignored, everything else still works
    job_id, pair, d = fit_job(synth.HARD, options={"not_an_option": 1})
    fit_mod.run(job_id)
    assert json.loads((d / "transform.json").read_text())["model"] == "similarity"


def test_spotcheck_uses_inverse_affine(fit_job):
    """MECH → STR must use the full affine inverse, not the diagonal."""
    from core import fit as fit_mod
    import core.spotcheck as sc

    job_id, pair, d = fit_job(synth.HARD)
    fit_mod.run(job_id)
    t = json.loads((d / "transform.json").read_text())

    aff = sc._affine_from(t)
    assert aff == Affine2D.from_json(t)
    inv = aff.inverse()

    for c in t["correspondences"]:
        p = tuple(c["str"])
        back = inv.apply(aff.apply(p))
        assert back[0] == pytest.approx(p[0], abs=1e-6)
        assert back[1] == pytest.approx(p[1], abs=1e-6)

    # a MECH saddle lands where the generator says it should — every one
    # of them, not just the first (the old `break` checked one of twelve)
    checked = 0
    for (letter, digit), mpt in pair.intersections_mech.items():
        spt = pair.intersections_str.get((letter, digit))
        if spt is None:
            continue
        assert inv.apply(tuple(mpt)) == pytest.approx(tuple(spt), abs=1e-6), \
            (letter, digit)
        checked += 1
    assert checked >= 10, f"only {checked} correspondences were checked"

    # ... while the pre-step-5 axis-aligned shortcut does not
    q = tuple(t["intersections"][0]["mech"])
    old = ((q[0] - t["tx"]) / t["scale_x"], (q[1] - t["ty"]) / t["scale_y"])
    assert not np.allclose(old, inv.apply(q))


# ─────────────────────────────────────────────────────────────────────────────
# the tolerance unit
# ─────────────────────────────────────────────────────────────────────────────

def test_median_spacing_is_adjacent_axis_distance(fx_synth_hard):
    """tau and the LOO tolerance are multiples of the *grid spacing*.

    Every gate in `solve` is relative to `FitInputs.spacing`, which the stage
    takes from `median_spacing(MECH grid)`.  That number must be the distance
    between adjacent parallel gridlines — not an along-line coordinate (all
    gridlines of a family start at the same corner, so their along-line
    offsets are all equal) and not the whole drawing extent.
    """
    from core.fit import median_spacing

    res = grid.detect_grid(ezdxf.readfile(str(fx_synth_hard.mech_path)),
                           PREFIX, AXES)
    # x spacings 6000/7500/6000 -> 6000, y spacings 5000/5000 -> 5000
    assert median_spacing(res) == pytest.approx(5500.0, rel=1e-9)

    # the STR drawing is the same grid rotated 7.5° and scaled by 0.001, so the
    # answer must be the same number in the STR drawing's own units
    sres = grid.detect_grid(ezdxf.readfile(str(fx_synth_hard.str_path)),
                            PREFIX, AXES)
    assert median_spacing(sres) == pytest.approx(5.5, rel=1e-9)


def test_tau_and_loo_tolerance_scale_with_the_grid(fx_synth_hard):
    """A 40 mm misalignment is an outlier at the real spacing."""
    inp, _ = grid_inputs(fx_synth_hard)
    assert inp.spacing == pytest.approx(5500.0, rel=1e-9)
    sol = solve(inp, FitOptions())
    assert sol["tau"] == pytest.approx(0.002 * 5500.0, rel=1e-9)     # 11 mm
    assert sol["loo_max"] <= 0.005 * 5500.0

    dst = inp.dst.copy()
    dst[3] += 40.0                       # one axis off by 40 mm
    sol = solve(FitInputs(**{**inp.__dict__, "dst": dst}), FitOptions())
    assert sol["outliers"] == [inp.labels[3]]
    assert sol["quality"]["status"] == "ok"


# ─────────────────────────────────────────────────────────────────────────────
# legacy axis summaries
# ─────────────────────────────────────────────────────────────────────────────

def test_legacy_x_y_labels_follow_the_vertical_horizontal_split(fit_job):
    """``x_labels`` are the gridlines positioned by X, ``y_labels`` those by Y.

    The classic convention puts the letters on the vertical gridlines, so for
    HARD (letters A-D on the verticals) x_labels must be A-D and y_labels the
    digits — regardless of which direction-cluster `core.grid` happened to
    number first.  Each row's ``mech``/``str`` value is that line's midpoint
    along the axis it is positioned by.
    """
    from core import fit as fit_mod

    job_id, pair, d = fit_job(synth.HARD)
    fit_mod.run(job_id)
    t = json.loads((d / "transform.json").read_text())
    axes = json.loads((d / "grid_mech.json").read_text())["axes"]
    mids = {lbl: a["midpoint"] for lbl, a in axes.items() if a["midpoint"]}

    def is_vertical(lbl):
        dx, dy = axes[lbl]["dir"]
        return abs(dy) > abs(dx)

    assert [r["label"] for r in t["x_labels"]] == [l for l in AXES if l in mids and is_vertical(l)]
    assert [r["label"] for r in t["y_labels"]] == [l for l in AXES if l in mids and not is_vertical(l)]
    assert t["x_labels"], "no vertical axis summary at all"
    for rows, col in ((t["x_labels"], 0), (t["y_labels"], 1)):
        for r in rows:
            assert r["mech"] == pytest.approx(mids[r["label"]][col], abs=1e-6), r["label"]