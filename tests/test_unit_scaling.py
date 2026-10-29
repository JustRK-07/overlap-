#!/usr/bin/env python3
"""tests/test_unit_scaling.py — the thresholds are millimetres, the
geometry is in drawing units.

Every geometric threshold in the project is specified in millimetres (the
plans say so), but the extractors work in the *drawing's own* units.  The
conversion was missing, and on a metre-scale STR drawing it is not a
rounding error:

* a 100-unit minimum length became 100 **metres**, so every member of
  the drawing was discarded — 26 beams and 7 gridlines in millimetres
  became 2 and **0** in metres;
* the same happened to the MECH-side size filters, so the intra-MECH
  detectors reported nothing at all;
* the spot-check hotspot radius (2000 units = 2 km) made every issue a
  hotspot.

These tests pin the behaviour: the same drawing, in millimetres and in
metres, must produce the same findings.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import ezdxf
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import synth                                   # noqa: E402
from core import mech_intra, spotcheck, str_intra  # noqa: E402


def _pair_in(tmp_path: Path, units: str):
    opts = synth.with_opts(synth.LEGACY_COMPATIBLE, str_units=units)
    return synth.build_pair(tmp_path / f"str_{units}", opts)


def test_the_str_member_extraction_is_the_same_in_mm_and_in_metres(tmp_path):
    """A12: a metre-scale STR must not lose its members."""
    mm = _pair_in(tmp_path, "mm")
    m = _pair_in(tmp_path, "m")

    doc_mm = ezdxf.readfile(str(mm.str_path))
    doc_m = ezdxf.readfile(str(m.str_path))
    assert doc_mm.header["$INSUNITS"] == 4
    assert doc_m.header["$INSUNITS"] == 6

    beams_mm, cols_mm = spotcheck._extract_str_beams_and_columns(doc_mm)
    beams_m, cols_m = spotcheck._extract_str_beams_and_columns(doc_m)

    assert beams_mm, "the mm fixture must have members"
    assert len(beams_mm) == len(beams_m), (
        f"mm found {len(beams_mm)} members, metres found {len(beams_m)}")
    assert len(cols_mm) == len(cols_m)


def test_the_str_gridline_extraction_is_the_same_in_mm_and_in_metres(tmp_path):
    """A12: a metre-scale STR must not lose its gridlines.

    This is the one that bit hardest: 7 gridlines in millimetres became
    **0** in metres, so the bay bounds collapsed and every saddle was
    reported as grid-misaligned with an infinite distance.
    """
    mm = _pair_in(tmp_path, "mm")
    m = _pair_in(tmp_path, "m")

    g_mm = spotcheck._extract_str_gridlines(ezdxf.readfile(str(mm.str_path)))
    g_m = spotcheck._extract_str_gridlines(ezdxf.readfile(str(m.str_path)))

    assert len(g_mm) >= 7, f"the mm fixture should have gridlines, got {g_mm}"
    assert len(g_mm) == len(g_m), (
        f"mm found {len(g_mm)} gridlines, metres found {len(g_m)}")


def test_the_str_member_extraction_in_str_intra_is_unit_invariant(tmp_path):
    """A12: the same for ``core/str_intra.py``."""
    mm = _pair_in(tmp_path, "mm")
    m = _pair_in(tmp_path, "m")

    beams_mm, cols_mm = str_intra._extract_members(
        ezdxf.readfile(str(mm.str_path)))
    beams_m, cols_m = str_intra._extract_members(
        ezdxf.readfile(str(m.str_path)))

    assert len(beams_mm) == len(beams_m)
    assert len(cols_mm) == len(cols_m)


def test_the_mech_footprint_extraction_is_unit_invariant(tmp_path):
    """A12: the MECH-side size filters are in millimetres too."""
    mm = _pair_in(tmp_path, "mm")
    m = _pair_in(tmp_path, "m")

    f_mm = mech_intra._find_mech_footprints(ezdxf.readfile(str(mm.str_path)))
    f_m = mech_intra._find_mech_footprints(ezdxf.readfile(str(m.str_path)))

    assert len(f_mm) == len(f_m), (
        f"mm found {len(f_mm)} footprints, metres found {len(f_m)}")


def test_the_saddle_size_bounds_are_unit_invariant(tmp_path):
    """A12: the MECH saddle bounds (200 mm .. 5000 mm) scale with the units."""
    mm = _pair_in(tmp_path, "mm")
    m = _pair_in(tmp_path, "m")

    s_mm = spotcheck._find_mech_saddles(ezdxf.readfile(str(mm.str_path)))
    s_m = spotcheck._find_mech_saddles(ezdxf.readfile(str(m.str_path)))

    assert len(s_mm) == len(s_m), (
        f"mm found {len(s_mm)} saddles, metres found {len(s_m)}")


def test_the_hotspot_radius_scales_with_the_mech_units():
    """A12: the 2000 mm hotspot radius is converted to drawing units."""
    from core.analyze import HOTSPOT_RADIUS_MM, _mech_mm_per_unit

    assert HOTSPOT_RADIUS_MM == 2000.0
    assert _mech_mm_per_unit({"units": {"mech": 4}}) == pytest.approx(1.0)
    assert _mech_mm_per_unit({"units": {"mech": 6}}) == pytest.approx(1000.0)
    # a drawing with no declared units is assumed to be millimetres
    assert _mech_mm_per_unit({}) == pytest.approx(1.0)
    assert _mech_mm_per_unit(None) == pytest.approx(1.0)


# ── A13 — the STR-intra coordinate space ───────────────────────────────────

def test_the_str_intra_issues_are_mapped_into_mech_world(fx_synth_job):
    """A13: the STR-intra detector works in STR coordinates.

    ``core/str_intra.py`` puts the clash point into ``mech_x``/``mech_y``
    (it reuses the field), but those coordinates are in the **STR**
    drawing's space.  Every other issue — and the DXF the markers are
    drawn on — is in MECH world, so the merged issue has to be mapped
    through the fit.
    """
    import pipelines
    from core import fit
    from jobs import job_dir

    job_id, _pair = fx_synth_job(synth.LEGACY_COMPATIBLE)
    pipelines.run_pipeline(job_id)
    d = job_dir(job_id)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": "TP104-", "axes": ["A", "B", "C", "D", "1", "2", "3"]}))
    fit.run(job_id)

    transform = json.loads((d / "transform.json").read_text())
    from core.transform2d import Affine2D
    affine = Affine2D.from_json(transform)

    # a STR-space point and the MECH-space point it maps to
    str_pt = (1234.0, 567.0)
    mech_pt = affine.apply(str_pt)

    (d / "stages" / "str_intra.json").write_text(json.dumps({
        "issues": [{
            "id": "STR-001", "type": "BEAM_COLUMN_CLASH",
            "severity": "CRITICAL",
            "mech_x": str_pt[0], "mech_y": str_pt[1],
            "str_block": "Part-BEAM", "distance": 10.0,
            "note": "a beam meets a column",
        }],
    }))

    spotcheck.run(job_id)

    merged = json.loads((d / "stages" / "spotcheck.json").read_text())
    issue = next(i for i in merged["issues"] if i["id"] == "STR-001")
    assert issue["mech_x"] == pytest.approx(mech_pt[0], abs=1e-6)
    assert issue["mech_y"] == pytest.approx(mech_pt[1], abs=1e-6)
    assert (issue["mech_x"], issue["mech_y"]) != str_pt, (
        "the issue must not keep its raw STR coordinates")
