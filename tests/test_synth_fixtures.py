#!/usr/bin/env python3
"""test_synth_fixtures.py — sanity tests for the synthetic pair generator
(tests/synth.py) and the geometry-truth harness (tests/geomtruth.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import ezdxf
import numpy as np
import pytest
from ezdxf import transform as ez_transform

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import geomtruth as gt  # noqa: E402
import synth  # noqa: E402

PRESETS = {"legacy": synth.LEGACY_COMPATIBLE, "hard": synth.HARD}


@pytest.fixture(params=sorted(PRESETS))
def pair(request, tmp_path):
    return synth.build_pair(tmp_path, PRESETS[request.param])


def test_numpy_available():
    assert np.__version__


def test_generator_truth_maps_str_grid_to_mech(pair):
    assert len(pair.intersections_mech) == 12
    pts = np.array(list(pair.intersections_mech.values()), dtype=float)
    extent = float(np.ptp(pts, axis=0).max()) + float(np.abs(pts).max())
    for k, mech_pt in pair.intersections_mech.items():
        mapped = pair.truth.apply(pair.intersections_str[k])
        err = np.hypot(mapped[0] - mech_pt[0], mapped[1] - mech_pt[1])
        assert err <= 1e-9 * extent, (k, err)


def test_generator_files_reopen_and_audit_clean(pair):
    for p in (pair.mech_path, pair.str_path):
        doc = ezdxf.readfile(str(p))
        aud = doc.audit()
        assert aud.has_errors is False, [e.message for e in aud.errors]


def test_generator_units_header(tmp_path):
    p = synth.build_pair(tmp_path / "m", synth.HARD)
    assert ezdxf.readfile(str(p.mech_path)).header["$INSUNITS"] == 4
    assert ezdxf.readfile(str(p.str_path)).header["$INSUNITS"] == 6
    p2 = synth.build_pair(tmp_path / "mm", synth.LEGACY_COMPATIBLE)
    assert ezdxf.readfile(str(p2.str_path)).header["$INSUNITS"] == 4
    assert p.truth.scale == pytest.approx(1000.0)
    assert p2.truth.scale == 1.0


def test_world_primitives_self_consistency(pair):
    """STR prims mapped by truth == prims after transform.inplace on a copy."""
    str_doc = ezdxf.readfile(str(pair.str_path))
    prims = gt.world_primitives(str_doc)
    assert len(prims) > 50
    mapped = gt.map_prims(prims, pair.truth)

    copy = ezdxf.readfile(str(pair.str_path))
    ez_transform.inplace(list(copy.modelspace()), pair.truth.matrix())
    # Compare in the MECH frame (mm): flatten at FLAT_MM there.
    actual = gt.world_primitives(copy, flat=gt.FLAT_MM)
    extent = float(np.abs(np.vstack([p.pts for p in mapped])).max())
    gt.assert_prims_match(actual, mapped, gt.tolerance(extent))


def test_grid_lines_helper_finds_mech_intersections(tmp_path):
    p = synth.build_pair(tmp_path, synth.LEGACY_COMPATIBLE)
    doc = ezdxf.readfile(str(p.mech_path))
    got = gt.grid_intersections_from_lines(doc, lambda l: l == synth.LAYER_MECH_GRID)
    assert len(got) == 12
    for exp in p.intersections_mech.values():
        assert min(np.hypot(g[0] - exp[0], g[1] - exp[1]) for g in got) < 1e-6


def test_assert_prims_match_detects_difference(tmp_path):
    p = synth.build_pair(tmp_path, synth.LEGACY_COMPATIBLE)
    doc = ezdxf.readfile(str(p.str_path))
    prims = gt.world_primitives(doc)
    shifted = [gt.Prim(q.kind, q.layer, q.pts + 5.0, q.text, q.height, q.rotation) for q in prims]
    with pytest.raises(AssertionError):
        gt.assert_prims_match(shifted, prims, 1e-3)


# ─────────────────────────────────────────────────────────────────────────────
# Mismatch scene (step 8b.1)
# ─────────────────────────────────────────────────────────────────────────────

MISMATCH_PRESETS = {"mismatch": synth.MISMATCH, "mismatch_hard": synth.MISMATCH_HARD}


def _world_anchors(path):
    """Exact MECH-world positions of circle centres, polyline vertices,
    line endpoints/midpoints and INSERT insertion points."""
    from ezdxf.math import Vec3

    from core.grid import walk_world

    doc = ezdxf.readfile(str(path))
    out = []

    def wp(item, p):
        v = item.m.transform(Vec3(float(p[0]), float(p[1]), 0.0))
        return (v.x, v.y)

    for item in walk_world(doc):
        e = item.entity
        t = e.dxftype()
        if t == "CIRCLE":
            out.append(wp(item, e.dxf.center))
        elif t == "LWPOLYLINE":
            out += [wp(item, q[:2]) for q in e.get_points("xy")]
        elif t == "LINE":
            a = wp(item, e.dxf.start)
            b = wp(item, e.dxf.end)
            out += [a, b, ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)]
        elif t == "INSERT":
            out.append(wp(item, e.dxf.insert))
    return np.asarray(out, dtype=float).reshape(-1, 2)


def _world_circles(path):
    """MECH-world (centre, radius) of every CIRCLE in the document."""
    from ezdxf.math import Vec3

    from core.grid import walk_world

    doc = ezdxf.readfile(str(path))
    out = []
    for item in walk_world(doc):
        e = item.entity
        if e.dxftype() != "CIRCLE":
            continue
        v = item.m.transform(Vec3(float(e.dxf.center[0]), float(e.dxf.center[1]), 0.0))
        r = item.m.transform_direction((e.dxf.radius, 0.0, 0.0)).magnitude
        out.append(((v.x, v.y), r))
    return out


def _nearest(anchors, pt):
    return float(np.hypot(anchors[:, 0] - pt[0], anchors[:, 1] - pt[1]).min())


def _str_anchors_in_mech_world(pair):
    """STR-file anchors mapped through the ground truth into MECH world."""
    from core.transform2d import Affine2D

    return Affine2D(*pair.truth.as_affine()).apply_many(_world_anchors(pair.str_path))


#: kinds whose finding location is a point that really exists in the drawing.
_POINT_KINDS = ("BOLT_HOLE_OFFSET", "MISSING_HOLE", "BOLT_OFF_PLATE", "PATTERN_OFFSET",
                "PATTERN_ROTATED", "SUPPORT_OFF_MEMBER", "SUPPORT_POINT_OFF_MEMBER",
                "MISSING_COUNTERPART", "OUTSIDE_OUTLINE", "EDGE_DISTANCE", "CLASH")
#: kinds where |offset_mm| is by definition the magnitude of the offset.
_OFFSET_EQ_MAGNITUDE = ("BOLT_HOLE_OFFSET", "PATTERN_OFFSET", "BOLT_OFF_PLATE",
                       "SUPPORT_OFF_MEMBER", "SUPPORT_POINT_OFF_MEMBER",
                       "OUTSIDE_OUTLINE", "EDGE_DISTANCE", "CLASH")


@pytest.mark.parametrize("preset", sorted(MISMATCH_PRESETS))
def test_mismatch_scene_geometry_matches_expected(preset, tmp_path):
    """Every ExpectedFinding points at geometry that is really in the files."""
    opts = MISMATCH_PRESETS[preset]
    pair = synth.build_pair(tmp_path / preset, opts)
    mech = _world_anchors(pair.mech_path)
    str_in_mech = _str_anchors_in_mech_world(pair)

    assert len(pair.mismatch_expected) == 13, "13 defects across the 18 cases"
    assert len(pair.mismatch_controls) == 8

    for exp in pair.mismatch_expected:
        if exp.case == "pattern_array":
            continue                      # a pattern centroid is not a drawn vertex
        pt = exp.location_mech
        assert _nearest(mech, pt) <= 1.0, (exp.case, exp.kind, pt)
        if exp.kind in ("BOLT_HOLE_OFFSET", "PATTERN_OFFSET", "PATTERN_ROTATED"):
            # STR counterpart sits exactly at MECH location + offset
            tgt = (pt[0] + exp.offset_mm[0], pt[1] + exp.offset_mm[1])
            assert _nearest(str_in_mech, tgt) <= 1.0, (exp.case, exp.kind, tgt)
        if exp.kind in _OFFSET_EQ_MAGNITUDE:
            assert float(np.hypot(*exp.offset_mm)) == pytest.approx(exp.magnitude_mm, abs=1e-9)

    # controls: nothing may be reported inside one
    for ctl in pair.mismatch_controls:
        d = min(float(np.hypot(e.location_mech[0] - ctl.center_mech[0],
                               e.location_mech[1] - ctl.center_mech[1]))
                for e in pair.mismatch_expected)
        assert d > ctl.radius_mm, (ctl.case, d)

    # the ctl_* cases of §6.1 are all present
    cases = {e.case for e in pair.mismatch_expected} | {c.case for c in pair.mismatch_controls}
    assert {c for c in synth.ALL_MISMATCH_CASES if c.startswith("ctl_")} <= cases


@pytest.mark.parametrize("preset", sorted(MISMATCH_PRESETS))
def test_mismatch_scene_blocks_are_not_cleaner_survivors(preset, tmp_path):
    """MECH scene blocks must NOT be A$C*/RTR* — the detector reads converted/."""
    opts = MISMATCH_PRESETS[preset]
    pair = synth.build_pair(tmp_path / preset, opts)

    mdoc = ezdxf.readfile(str(pair.mech_path))
    m_names = {b.name for b in mdoc.blocks}
    scene_mech = {n for n in m_names
                  if n in synth.MECH_SCENE_BLOCKS or n.startswith("BOLT-M20-4-X")}
    assert scene_mech == set(synth.MECH_SCENE_BLOCKS) | {
        n for n in m_names if n.startswith("BOLT-M20-4-X")}
    for n in scene_mech:
        assert not n.startswith("A$C") and not n.startswith("RTR"), n

    sdoc = ezdxf.readfile(str(pair.str_path))
    s_names = {b.name for b in sdoc.blocks}
    assert set(synth.STR_SCENE_BLOCKS) <= s_names
    for n in synth.STR_SCENE_BLOCKS:
        assert n.startswith("Part-"), n


def test_pattern_array_truth_is_the_authored_shift(tmp_path):
    """pattern_array = 4x4 ctl_bp4; element (2,1) carries the extra +8 mm."""
    pair = synth.build_pair(tmp_path / "g", synth.MISMATCH_GLOBAL)
    assert pair.mismatch_controls == ()
    assert len(pair.mismatch_expected) == 16
    mags = sorted(round(e.magnitude_mm, 4) for e in pair.mismatch_expected)
    assert mags.count(25.0) == 15
    assert mags[-1] == pytest.approx(26.2488, abs=1e-3)   # sqrt(25^2 + 8^2)

    # each location is the centroid of its own 4 MECH bolts at (+-100, +-100),
    # i.e. exactly sqrt(2)*100 mm from each of them
    circles = np.asarray([c for c, _ in _world_circles(pair.mech_path)], dtype=float)
    bolt_r = 100.0 * 2.0 ** 0.5
    for exp in pair.mismatch_expected:
        d = np.hypot(circles[:, 0] - exp.location_mech[0], circles[:, 1] - exp.location_mech[1])
        assert int(np.count_nonzero(np.abs(d - bolt_r) < 1e-6)) == 4, exp.offset_mm


@pytest.mark.parametrize("preset", sorted(MISMATCH_PRESETS))
def test_mismatch_scene_does_not_break_grid(preset, tmp_path):
    """The scene must be invisible to step 4, for both files and both presets."""
    from core.grid import detect_grid, intersections

    opts = MISMATCH_PRESETS[preset]
    with_scene = synth.build_pair(tmp_path / "with", opts)
    without = synth.build_pair(tmp_path / "without", synth.with_opts(opts, mismatch_cases=()))

    suffixes = list(opts.x_labels) + list(opts.y_labels)
    for attr in ("mech_path", "str_path"):
        a = detect_grid(ezdxf.readfile(str(getattr(with_scene, attr))), opts.prefix, suffixes)
        b = detect_grid(ezdxf.readfile(str(getattr(without, attr))), opts.prefix, suffixes)
        assert [a.axes[s].status for s in suffixes] == ["ok"] * len(suffixes), attr
        assert [b.axes[s].status for s in suffixes] == ["ok"] * len(suffixes), attr
        assert a.families == b.families
        assert a.extent > b.extent          # the scene really is in there
        ia, ib = intersections(a), intersections(b)
        assert set(ia) == set(ib) and len(ia) == 12
        for k, v in ia.items():
            assert (v - ib[k]).magnitude <= 1e-6, (attr, k)

    # the scene grew the drawing, so every scene LINE must stay under the
    # 0.15 x extent filter of merge_collinear (§6.1's constraint)
    str_doc = ezdxf.readfile(str(with_scene.str_path))
    extent = detect_grid(str_doc, opts.prefix, suffixes).extent
    from core.transform2d import Affine2D

    aff = Affine2D(*with_scene.truth.as_affine())
    x_scene = opts.scene_origin[0] - 1000.0
    n_scene_lines = 0
    for e in str_doc.modelspace():
        if e.dxftype() != "LINE":
            continue
        a, b = e.dxf.start, e.dxf.end
        mid = aff.apply(((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0))
        if mid[0] < x_scene + with_scene.truth.tx:      # not in the scene
            continue
        n_scene_lines += 1
        assert float(np.hypot(b[0] - a[0], b[1] - a[1])) < 0.15 * extent, (a, b)
    assert n_scene_lines == 6, "three two-line bands are drawn in the scene"
