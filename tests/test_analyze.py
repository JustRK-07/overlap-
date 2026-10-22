#!/usr/bin/env python3
"""test_analyze.py — Unit tests for core.analyze.

Covers:
  - bbox_diagonal: monotonic with extent (synthetic DXF)
  - normalize_depth: clamps to [0, 1]
  - analyze_pair (heavy): full Slice-1 fixture end-to-end.

Light tests skip the heavy fixture (mark as `slow` for the heavy ones).
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path

import ezdxf
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from core.analyze import (  # noqa: E402
    DEPTH_FRACTION,
    analyze_pair,
    bbox_diagonal,
    normalize_depth,
)
from jobs import job_dir  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# bbox_diagonal — synthetic DXF
# ─────────────────────────────────────────────────────────────────────────────

def test_bbox_diagonal_zero_for_empty_doc():
    """An empty modelspace → 0.0 (caller treats as fallback)."""
    with tempfile.TemporaryDirectory() as tmp:
        dxf = Path(tmp) / "empty.dxf"
        doc = ezdxf.new()
        doc.saveas(str(dxf))
        assert bbox_diagonal(dxf) == 0.0


def test_bbox_diagonal_monotonic_with_extent():
    """A drawing with extent 0..1000 in both axes → diag = sqrt(2) × 1000."""
    with tempfile.TemporaryDirectory() as tmp:
        dxf = Path(tmp) / "monotonic.dxf"
        doc = ezdxf.new()
        msp = doc.modelspace()
        # One LINE spanning the bbox 0..1000 × 0..1000.
        msp.add_line((0.0, 0.0), (1000.0, 1000.0))
        doc.saveas(str(dxf))
        d = bbox_diagonal(dxf)
        assert abs(d - math.hypot(1000.0, 1000.0)) < 1e-6


def test_bbox_diagonal_picks_largest_extent():
    """Add a small LWPOLYLINE; bbox_diagonal should reflect the
    largest extent across every entity, not just the first."""
    with tempfile.TemporaryDirectory() as tmp:
        dxf = Path(tmp) / "largest.dxf"
        doc = ezdxf.new()
        msp = doc.modelspace()
        msp.add_line((0, 0), (50, 50))                    # small
        msp.add_lwpolyline([(0, 0), (5000, 0), (5000, 5000)], format="xy")  # big
        doc.saveas(str(dxf))
        d = bbox_diagonal(dxf)
        # max extent: 5000 × 5000 → diag = hypot(5000, 5000) = 7071.07...
        assert abs(d - math.hypot(5000.0, 5000.0)) < 1e-3


# ─────────────────────────────────────────────────────────────────────────────
# normalize_depth — pure function
# ─────────────────────────────────────────────────────────────────────────────

def test_normalize_depth_zero_at_zero_residual():
    assert normalize_depth(0.0, 2000.0) == 0.0


def test_normalize_depth_one_at_fraction_target():
    """residual = DEPTH_FRACTION × diag → 1.0."""
    diag = 10_000.0
    target = DEPTH_FRACTION * diag  # = 50.0
    assert normalize_depth(target, diag) == pytest.approx(1.0)


def test_normalize_depth_clamped_above_fraction():
    """A 10× residual clamps to 1.0 (no overflow)."""
    diag = 10_000.0
    big = 10 * DEPTH_FRACTION * diag  # = 500.0
    assert normalize_depth(big, diag) == 1.0


def test_normalize_depth_handles_zero_diagonal():
    """Diag=0 must NOT raise — falls back to FALLBACK_DIAGONAL."""
    assert normalize_depth(0.0, 0.0) == 0.0
    assert normalize_depth(1.0, 0.0) == 1.0  # clamped


# ─────────────────────────────────────────────────────────────────────────────
# analyze_pair — heavy (end-to-end via Slice-1 fixture)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
def test_analyze_pair_smoke(fx_job_with_cleaned_dxfs, fx_tp104_dxfs):
    """Run pair_runner, then analyze_pair on the resulting pair dir.

    Asserts:
      - intersections populated (TP-104 has 3×3 = 9 grid intersections)
      - every normalized_depth in [0, 1]
      - severity_breakdown keys ⊆ {CRITICAL, WARNING, INFO}
      - bbox_diagonals has both mech and str with positive values
      - rms_residual/max_residual match transform.json
    """
    from core.pair_runner import encode_pair_id, run_pair, pair_root
    job_id = fx_job_with_cleaned_dxfs
    mech, struct = fx_tp104_dxfs
    pair_id = encode_pair_id(mech.name, struct.name)
    run_pair(
        job_id, mech_filename=mech.name, str_filename=struct.name,
        pair_id=pair_id, prefix="TP104-",
        axes=["A", "B", "C", "D", "1", "2", "3"],
    )

    a = analyze_pair(job_id, pair_id=pair_id)

    assert a["pair_id"] == pair_id
    assert a["mech_file"] == mech.name
    assert a["str_file"] == struct.name
    assert isinstance(a["intersections"], list)
    assert len(a["intersections"]) >= 1, "TP-104 should produce ≥1 intersection"
    for ix in a["intersections"]:
        assert 0.0 <= ix["normalized_depth"] <= 1.0, ix
        assert ix["label"]   # non-empty string
        assert isinstance(ix["mech"], list) and len(ix["mech"]) == 2

    valid_severities = {"CRITICAL", "WARNING", "INFO", ""}
    for sev in (a["severity_breakdown"] or {}).keys():
        assert sev in valid_severities, f"unexpected severity: {sev}"

    assert a["bbox_diagonals"]["mech"] > 0
    assert a["bbox_diagonals"]["str"]  > 0

    # Cross-check against transform.json directly.
    t = json.loads((pair_root(job_id, pair_id) / "transform.json").read_text())
    assert a["rms_residual"] == t["rms_residual"]
    assert a["max_residual"] == t["max_residual"]


@pytest.mark.slow
def test_analyze_pair_legacy_layout(fx_job_with_cleaned_dxfs, fx_tp104_dxfs, monkeypatch):
    """analyze_pair with pair_id=None reads the legacy single-pair
    layout (jobs/<id>/transform.json, jobs/<id>/stages/spotcheck.json).

    We simulate the legacy layout by lifting the pair's artifacts up.
    """
    from core.pair_runner import encode_pair_id, run_pair, pair_root
    job_id = fx_job_with_cleaned_dxfs
    mech, struct = fx_tp104_dxfs
    pair_id = encode_pair_id(mech.name, struct.name)
    run_pair(
        job_id, mech_filename=mech.name, str_filename=struct.name,
        pair_id=pair_id, prefix="TP104-",
        axes=["A", "B", "C", "D", "1", "2", "3"],
    )

    pair_root_dir = pair_root(job_id, pair_id)
    d = job_dir(job_id)
    # Lift artifacts up to the legacy paths.
    import shutil
    shutil.copyfile(pair_root_dir / "transform.json",     d / "transform.json")
    (d / "stages").mkdir(exist_ok=True)
    shutil.copyfile(pair_root_dir / "stages" / "spotcheck.json",
                    d / "stages" / "spotcheck.json")
    # Also lift the cleaned files so analyze_pair finds them.
    for src in pair_root_dir.glob("cleaned/*.dxf"):
        shutil.copyfile(src, d / "cleaned" / src.name)

    a = analyze_pair(job_id)  # pair_id=None — legacy layout
    assert a["pair_id"] == "_legacy"
    assert a["n_issues"] >= 1
    assert all(0.0 <= ix["normalized_depth"] <= 1.0 for ix in a["intersections"])
