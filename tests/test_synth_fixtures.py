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
