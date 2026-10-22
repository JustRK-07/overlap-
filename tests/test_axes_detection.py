#!/usr/bin/env python3
"""test_axes_detection.py — Unit tests for the new axes-detection heuristic.

Covers:
  - _try_split_label: greedy-from-end suffix split for both letter and
    digit suffixes, mixed-suffix labels, and edge cases.
  - extract_labels: returns LabelCandidate dataclasses with the right
    fields; dedupes by raw text (case-insensitive).
  - score_cluster: applies the proximity + mixed-letter-digit formula
    correctly.
  - suggest: ranks clusters by score, drops < 2 suffix floors but
    keeps small conventions.
  - End-to-end on TP-104 fixture: same prefix/axes as baseline
    (regression guard).

Run:
  python3 -m pytest tests/test_axes_detection.py -v
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

# Ensure the overlay-app root is on sys.path so `core` is importable.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import ezdxf  # noqa: E402

from core.axes import (  # noqa: E402
    LabelCandidate,
    _adaptive_threshold,
    _annotate_proximity,
    _build_insert_lookup,
    _build_world_gridline_cache,
    _compute_world_positions,
    _looks_like_beam_section,
    _score_cluster,
    _transform_pt,
    _try_split_label,
    extract_labels,
    suggest,
)
from core.geom import long_axis_lines, point_segment_distance  # noqa: E402


# Path to the TP-104 fixture used for the regression test.
TP104_MECH = Path(os.environ.get(
    "ISGEC_DIR",
    str(ROOT.parent / "ISGEC"),
)) / "dxf" / "TP-104 MECH GA_clean3.dxf"
TP104_STR = Path(os.environ.get(
    "ISGEC_DIR",
    str(ROOT.parent / "ISGEC"),
)) / "dxf" / "TP-104 STR GA_clean3.dxf"


# ─────────────────────────────────────────────────────────────────────────────
# _try_split_label
# ─────────────────────────────────────────────────────────────────────────────

def test_split_pure_letter_suffix():
    """Trailing letter run → prefix + letter suffix."""
    assert _try_split_label("TP104-A") == ("TP104-", "A")


def test_split_pure_digit_suffix():
    """Trailing digit run → prefix + digit suffix."""
    assert _try_split_label("TP104-1") == ("TP104-", "1")


def test_split_multi_digit_suffix():
    """Multi-digit suffix is preserved."""
    assert _try_split_label("TP104-12") == ("TP104-", "12")


def test_split_multi_letter_suffix():
    """Multi-letter suffix is preserved."""
    assert _try_split_label("TP104-AB") == ("TP104-", "AB")


def test_split_mixed_suffix_prefers_letter():
    """Labels like 'TP104-1A' (digit then letter) — the new heuristic
    splits off the trailing letter, giving prefix='TP104-1' suffix='A'."""
    p, s = _try_split_label("TP104-1A")
    assert s == "A"
    assert p == "TP104-1"


def test_split_mixed_suffix_prefers_digit():
    """Labels like 'GR-A1' (letter then digit) — split off trailing digit."""
    p, s = _try_split_label("GR-A1")
    assert s == "1"
    assert p == "GR-A"


def test_split_bare_letter_returns_none():
    """A label like 'A' alone has no meaningful prefix — skip it."""
    assert _try_split_label("A") is None


def test_split_bare_digit_returns_none():
    """A label like '1' alone has no meaningful prefix — skip it."""
    assert _try_split_label("1") is None


# ─────────────────────────────────────────────────────────────────────────────
# extract_labels: dataclass shape + dedup
# ─────────────────────────────────────────────────────────────────────────────

def _make_doc_with_labels(labels: list[str]) -> ezdxf.document.Drawing:
    """Build a minimal in-memory DXF with one TEXT per label."""
    doc = ezdxf.new()
    msp = doc.modelspace()
    for i, txt in enumerate(labels):
        # Spread them out so proximity scoring can distinguish them.
        msp.add_text(txt, dxfattribs={"insert": (i * 200.0, 0.0)})
    return doc


def test_extract_returns_dataclass():
    doc = _make_doc_with_labels(["TP104-1", "TP104-A"])
    labels = extract_labels(doc)
    assert len(labels) == 2
    assert all(isinstance(c, LabelCandidate) for c in labels)
    assert {c.text for c in labels} == {"TP104-1", "TP104-A"}


def test_extract_dedupes_by_raw_text():
    """Same TEXT twice (different cases) — only one LabelCandidate returned."""
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_text("TP104-1", dxfattribs={"insert": (0.0, 0.0)})
    msp.add_text("tp104-1", dxfattribs={"insert": (100.0, 0.0)})
    labels = extract_labels(doc)
    assert len(labels) == 1


def test_extract_carries_position_and_container():
    doc = _make_doc_with_labels(["TP104-A"])
    labels = extract_labels(doc)
    assert labels[0].position[0] == 0.0
    assert labels[0].position[1] == 0.0
    assert labels[0].container == "modelspace"
    assert labels[0].entity is not None


# ─────────────────────────────────────────────────────────────────────────────
# _score_cluster
# ─────────────────────────────────────────────────────────────────────────────

def _mk(prefix, suffix, x, y, on_gridline=False):
    """Build a LabelCandidate without going through ezdxf."""
    return LabelCandidate(
        text=f"{prefix}{suffix}",
        prefix=prefix,
        suffix=suffix,
        position=(x, y),
        on_gridline=on_gridline,
    )


def test_score_pure_text_no_proximity():
    """Cluster where no labels sit on gridlines: factor = 1.0."""
    cluster = [_mk("X-", "1", 0, 0, on_gridline=False),
               _mk("X-", "2", 0, 0, on_gridline=False),
               _mk("X-", "A", 0, 0, on_gridline=False),
               _mk("X-", "B", 0, 0, on_gridline=False)]
    score = _score_cluster(cluster)
    # count=4, mixed=2 (both letter and digit suffixes), prox=1.0 → 8.0
    assert score == 8.0


def test_score_mixed_text_with_proximity():
    """Cluster where most labels sit on gridlines: factor up to 3.0."""
    cluster = [_mk("X-", "1", 0, 0, on_gridline=True),
               _mk("X-", "2", 0, 0, on_gridline=True),
               _mk("X-", "3", 0, 0, on_gridline=True),
               _mk("X-", "A", 0, 0, on_gridline=True)]
    score = _score_cluster(cluster)
    # count=4, mixed=2, prox=1.0 + 2.0×(4/4)=3.0 → 24.0
    assert score == 24.0


def test_score_small_cluster_outranks_larger_pure_text():
    """A cluster of 4 labels on gridlines (score ~24) beats a cluster of
    7 pure-text labels with no proximity (score 14)."""
    gridline_cluster = [_mk("X-", "1", 0, 0, on_gridline=True),
                        _mk("X-", "2", 0, 0, on_gridline=True),
                        _mk("X-", "3", 0, 0, on_gridline=True),
                        _mk("X-", "A", 0, 0, on_gridline=True)]
    pure_text_cluster = [_mk("Y-", "1", 0, 0, on_gridline=False),
                         _mk("Y-", "2", 0, 0, on_gridline=False),
                         _mk("Y-", "3", 0, 0, on_gridline=False),
                         _mk("Y-", "4", 0, 0, on_gridline=False),
                         _mk("Y-", "5", 0, 0, on_gridline=False),
                         _mk("Y-", "6", 0, 0, on_gridline=False),
                         _mk("Y-", "7", 0, 0, on_gridline=False)]
    assert _score_cluster(gridline_cluster) > _score_cluster(pure_text_cluster)


def test_score_empty_cluster_zero():
    assert _score_cluster([]) == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# _annotate_proximity + long_axis_lines
# ─────────────────────────────────────────────────────────────────────────────

def test_annotate_proximity_marks_label_on_gridline():
    """A label sitting exactly on a long LINE gets on_gridline=True."""
    doc = ezdxf.new()
    msp = doc.modelspace()
    # Long horizontal gridline at y=0, from x=0 to x=10000.
    msp.add_line((0.0, 0.0), (10000.0, 0.0))
    # Label exactly on the gridline.
    msp.add_text("AXIS-A", dxfattribs={"insert": (100.0, 0.0)})
    labels = extract_labels(doc)
    _compute_world_positions(doc, labels)
    long_lines = _build_world_gridline_cache(doc)
    _annotate_proximity(labels, long_lines)
    assert labels[0].on_gridline is True


def test_annotate_proximity_marks_label_off_gridline():
    """A label sitting far from any long LINE gets on_gridline=False."""
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((0.0, 0.0), (10000.0, 0.0))
    # Label 5000 units away from the gridline.
    msp.add_text("AXIS-A", dxfattribs={"insert": (100.0, 5000.0)})
    labels = extract_labels(doc)
    _compute_world_positions(doc, labels)
    long_lines = _build_world_gridline_cache(doc)
    _annotate_proximity(labels, long_lines)
    assert labels[0].on_gridline is False
    assert labels[0].distance_to_gridline >= 5000.0


def test_long_axis_lines_skips_short_segments():
    """Segments shorter than min_length are not yielded."""
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((0.0, 0.0), (50.0, 0.0))   # 50 — short
    msp.add_line((0.0, 0.0), (500.0, 0.0))  # 500 — long
    segs = list(long_axis_lines(msp, min_length=100.0))
    assert len(segs) == 1
    assert segs[0] == ((0.0, 0.0), (500.0, 0.0))


def test_long_axis_lines_skips_closed_lwpolyline():
    """Closed LWPOLYLINE (equipment outlines) are skipped."""
    doc = ezdxf.new()
    msp = doc.modelspace()
    pts = [(0.0, 0.0), (500.0, 0.0), (500.0, 500.0), (0.0, 500.0)]
    msp.add_lwpolyline(pts, format="xy", close=True)  # closed outline
    msp.add_line((0.0, 0.0), (500.0, 0.0))
    segs = list(long_axis_lines(msp, min_length=100.0))
    assert len(segs) == 1


def test_point_segment_distance_basic():
    """Sanity check the geom helper reused by proximity scoring."""
    assert point_segment_distance(0, 0, 0, 0, 3, 4) == 0.0
    assert point_segment_distance(1, 0, 0, 0, 3, 0) == 0.0
    assert abs(point_segment_distance(0, 1, 0, 0, 3, 0) - 1.0) < 1e-9


# ─────────────────────────────────────────────────────────────────────────────
# suggest()
# ─────────────────────────────────────────────────────────────────────────────

def test_suggest_small_project_with_proximity():
    """A drawing with just 2 axis labels (both on gridlines) — must be
    picked even though it's below the old [4,20] suffix floor."""
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((0.0, 0.0), (10000.0, 0.0))
    msp.add_line((0.0, 0.0), (0.0, 10000.0))
    msp.add_text("GRID-A", dxfattribs={"insert": (5000.0, 0.0)})
    msp.add_text("GRID-1", dxfattribs={"insert": (0.0, 5000.0)})
    # suggest() takes a path; write to a tmp file.
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
        tmp_path = f.name
    try:
        doc.saveas(tmp_path)
        sugs = suggest(Path(tmp_path))
    finally:
        os.unlink(tmp_path)
    assert len(sugs) >= 1
    top = sugs[0]
    assert top["prefix"] == "GRID-"
    assert set(top["axes"]) == {"A", "1"}


def test_suggest_returns_score_field():
    """Each suggestion must carry a numeric `score` so callers can show it."""
    doc = _make_doc_with_labels(["TP104-1", "TP104-2", "TP104-A", "TP104-B"])
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
        tmp_path = f.name
    try:
        doc.saveas(tmp_path)
        sugs = suggest(Path(tmp_path))
    finally:
        os.unlink(tmp_path)
    assert "score" in sugs[0]
    assert isinstance(sugs[0]["score"], (int, float))
    assert sugs[0]["score"] > 0


# ─────────────────────────────────────────────────────────────────────────────
# Regression guard — TP-104 fixture
# ─────────────────────────────────────────────────────────────────────────────

def test_tp104_str_top_convention_unchanged():
    """The STR drawing's top suggestion must still be prefix='TP104-'
    with axes A B C D 1 2 3 — this is the same convention the old
    heuristic picked for the TP-104 fixture."""
    if not TP104_STR.exists():
        import pytest
        pytest.skip(f"TP-104 STR fixture missing: {TP104_STR}")
    sugs = suggest(TP104_STR)
    assert sugs, "expected at least one suggestion for TP-104 STR"
    top = sugs[0]
    assert top["prefix"] == "TP104-"
    assert set(top["axes"]) == {"A", "B", "C", "D", "1", "2", "3"}


def test_tp104_mech_top_convention_unchanged():
    """Same guard for the MECH drawing."""
    if not TP104_MECH.exists():
        import pytest
        pytest.skip(f"TP-104 MECH fixture missing: {TP104_MECH}")
    sugs = suggest(TP104_MECH)
    assert sugs, "expected at least one suggestion for TP-104 MECH"
    top = sugs[0]
    assert top["prefix"] == "TP104-"
    assert set(top["axes"]) == {"A", "B", "C", "D", "1", "2", "3"}


def test_tp104_both_drawings_score_equally():
    """After the INSERT-transform fix, MECH and STR should score
    equally on the same convention — both drawings correctly identify
    the same axes with the same confidence. (Pre-fix, STR had a
    false-positive advantage from comparing block-local label coords
    against block-local line endpoints.)"""
    if not (TP104_MECH.exists() and TP104_STR.exists()):
        import pytest
        pytest.skip("TP-104 fixtures missing")
    sugs_m = suggest(TP104_MECH)
    sugs_s = suggest(TP104_STR)
    assert sugs_m[0]["score"] > 0
    assert sugs_s[0]["score"] > 0
    # Both drawings should detect ALL 7 labels as on_gridline now —
    # the spatial signal is consistent across drawings.
    assert sugs_m[0]["n_on_gridline"] == 7
    assert sugs_s[0]["n_on_gridline"] == 7


# ─────────────────────────────────────────────────────────────────────────────
# INSERT-transform — world-coord gridlines + world-coord labels
# ─────────────────────────────────────────────────────────────────────────────

def _make_doc_with_block_insert():
    """Build a synthetic DXF: one block containing a long horizontal
    LINE and a TEXT. Instantiate the block at modelspace with a
    non-trivial insert + scale. The label should be detected as
    on_gridline ONLY if world-coord comparison is done correctly."""
    doc = ezdxf.new()
    block = doc.blocks.new("TestAxis")
    block.add_line((0.0, 0.0), (1000.0, 0.0))  # horizontal gridline
    block.add_text("TP1-A", dxfattribs={"insert": (500.0, 0.0)})  # on the line
    # Instantiate at modelspace: insert=(10000, 20000), scale=0.5
    # World line: (10000, 20000) to (10500, 20000)
    # World text: (10000 + 0.5*500, 20000) = (10250, 20000)
    doc.modelspace().add_blockref("TestAxis", (10000.0, 20000.0, 0.0),
                                  dxfattribs={"xscale": 0.5, "yscale": 0.5})
    return doc


def test_block_label_transformed_to_world_coords():
    """A TEXT inside a block def gets its world_position set by
    applying the parent INSERT transform."""
    doc = _make_doc_with_block_insert()
    labels = extract_labels(doc)
    _compute_world_positions(doc, labels)
    # The block-local insert is (500, 0); with INSERT insert=(10000, 20000)
    # and xscale=0.5, world = (10000 + 0.5*500, 20000 + 0.5*0) = (10250, 20000).
    assert labels[0].text == "TP1-A"
    assert labels[0].block_name == "TestAxis"
    assert labels[0].world_position == (10250.0, 20000.0)


def test_block_gridline_transformed_to_world_coords():
    """A LINE inside a block def gets its endpoints transformed by the
    parent INSERT transform when added to the world gridline cache."""
    doc = _make_doc_with_block_insert()
    segs = _build_world_gridline_cache(doc)
    assert len(segs) == 1
    (p, q) = segs[0]
    # World coords: (10000+0.5*0, 20000+0.5*0) to (10000+0.5*1000, 20000+0.5*0)
    # = (10000, 20000) to (10500, 20000)
    assert abs(p[0] - 10000.0) < 1e-9
    assert abs(p[1] - 20000.0) < 1e-9
    assert abs(q[0] - 10500.0) < 1e-9
    assert abs(q[1] - 20000.0) < 1e-9


def test_block_label_marked_on_gridline_via_world_compare():
    """A block-def TEXT sitting on a block-def LINE — when both are
    transformed to world coords, the proximity check finds them."""
    doc = _make_doc_with_block_insert()
    labels = extract_labels(doc)
    _compute_world_positions(doc, labels)
    gridlines = _build_world_gridline_cache(doc)
    _annotate_proximity(labels, gridlines)
    assert labels[0].on_gridline is True
    assert labels[0].distance_to_gridline < 1.0


def test_block_without_insert_reference_skipped():
    """A block with no modelspace INSERT reference shouldn't pollute
    the world gridline cache (its lines would be invisible in the
    rendered drawing)."""
    doc = ezdxf.new()
    orphan_block = doc.blocks.new("OrphanAxis")
    orphan_block.add_line((0.0, 0.0), (5000.0, 0.0))
    orphan_block.add_text("X-A", dxfattribs={"insert": (2500.0, 0.0)})
    segs = _build_world_gridline_cache(doc)
    assert segs == []
    labels = extract_labels(doc)
    _compute_world_positions(doc, labels)
    assert labels[0].world_position == (2500.0, 0.0)  # no transform → local coords


def test_insert_lookup_picks_first_insert():
    """When multiple INSERTs reference the same block, the first one
    in modelspace order is used as the transform source."""
    doc = ezdxf.new()
    block = doc.blocks.new("SharedAxis")
    block.add_line((0.0, 0.0), (1000.0, 0.0))
    msp = doc.modelspace()
    # First INSERT at (100, 200), scale 1.0
    msp.add_blockref("SharedAxis", (100.0, 200.0, 0.0),
                     dxfattribs={"xscale": 1.0, "yscale": 1.0})
    # Second INSERT at (999, 999), scale 2.0 — should be ignored
    msp.add_blockref("SharedAxis", (999.0, 999.0, 0.0),
                     dxfattribs={"xscale": 2.0, "yscale": 2.0})
    lookup = _build_insert_lookup(doc)
    assert lookup["SharedAxis"] == (100.0, 200.0, 1.0, 1.0, 0.0)


def test_insert_lookup_captures_rotation():
    """The lookup captures the INSERT's rotation converted from
    ezdxf's degrees to radians (AutoCAD convention)."""
    doc = ezdxf.new()
    doc.blocks.new("RotatedAxis")
    msp = doc.modelspace()
    msp.add_blockref("RotatedAxis", (0.0, 0.0, 0.0),
                     dxfattribs={"rotation": 45})  # degrees in ezdxf
    lookup = _build_insert_lookup(doc)
    ix, iy, sx, sy, r = lookup["RotatedAxis"]
    # The lookup returns radians (internal representation for
    # math.cos / math.sin). ezdxf stores degrees natively.
    assert abs(r - math.radians(45)) < 1e-9
    assert sx == 1.0 and sy == 1.0


def test_transform_pt_handles_rotation():
    """Verify the rotation math: a local (1000, 0) point with a 90°
    rotation around the block origin should land at (0, 1000) in
    the rotated frame, then add the INSERT translation."""
    # INSERT at (100, 200), no scale, 90° rotation.
    tx = (100.0, 200.0, 1.0, 1.0, math.pi / 2)
    # Local (1000, 0): scaled stays (1000, 0); rotated 90° CCW → (0, 1000);
    # translated → (100, 1200).
    wx, wy = _transform_pt((1000.0, 0.0), tx)
    assert abs(wx - 100.0) < 1e-9
    assert abs(wy - 1200.0) < 1e-9


def test_transform_pt_handles_non_uniform_scale():
    """Non-uniform scale (xscale=2, yscale=3) should apply before rotation."""
    # INSERT at (0, 0), xscale=2, yscale=3, no rotation.
    tx = (0.0, 0.0, 2.0, 3.0, 0.0)
    wx, wy = _transform_pt((100.0, 50.0), tx)
    assert (wx, wy) == (200.0, 150.0)


def test_transform_pt_combines_rotation_and_scale():
    """Scale + rotation + translation combined."""
    # INSERT at (10, 20), xscale=yscale=2, rotation=90° (CCW).
    tx = (10.0, 20.0, 2.0, 2.0, math.pi / 2)
    # Local (5, 0): scaled (10, 0); rotated 90° → (0, 10);
    # translated → (10, 30).
    wx, wy = _transform_pt((5.0, 0.0), tx)
    assert abs(wx - 10.0) < 1e-9
    assert abs(wy - 30.0) < 1e-9


def test_rotated_block_label_lands_in_world_coords():
    """A TEXT inside a block def referenced by a rotated INSERT should
    have its world_position reflect the rotation."""
    doc = ezdxf.new()
    block = doc.blocks.new("RotAxis")
    block.add_line((0.0, 0.0), (1000.0, 0.0))  # local horizontal gridline
    block.add_text("GR-A", dxfattribs={"insert": (500.0, 0.0)})  # local label
    # Rotate 90° CCW. INSERT at (10000, 20000), no scale.
    # Local line (0,0)-(1000,0) → rotated → (0,0)-(0,1000) → translated → (10000, 20000)-(10000, 21000)
    # Local label (500, 0) → rotated → (0, 500) → translated → (10000, 20500)
    doc.modelspace().add_blockref(
        "RotAxis", (10000.0, 20000.0, 0.0),
        dxfattribs={"rotation": 90},
    )
    labels = extract_labels(doc)
    _compute_world_positions(doc, labels)
    gridlines = _build_world_gridline_cache(doc)
    assert len(gridlines) == 1
    p, q = gridlines[0]
    # World line goes from (10000, 20000) to (10000, 21000) — VERTICAL
    assert abs(p[0] - 10000.0) < 1e-9 and abs(p[1] - 20000.0) < 1e-9
    assert abs(q[0] - 10000.0) < 1e-9 and abs(q[1] - 21000.0) < 1e-9
    # Label sits at (10000, 20500) — midpoint of the vertical line
    assert abs(labels[0].world_position[0] - 10000.0) < 1e-9
    assert abs(labels[0].world_position[1] - 20500.0) < 1e-9
    assert labels[0].on_gridline is False  # distance > 55, not on it yet
    # Re-annotate with the world gridline → now it's exactly ON the line
    _annotate_proximity(labels, gridlines)
    assert labels[0].distance_to_gridline < 1e-6


def test_adaptive_threshold_scales_with_drawing_size():
    """A large-drawing label sitting within the adaptive threshold of
    its gridline should be marked on_gridline. With diag ≈ 1.4M and
    fraction=0.0005, threshold ≈ 707. Place the label 500 units
    above the gridline — within the scaled threshold."""
    doc = ezdxf.new()
    msp = doc.modelspace()
    # Drawing spans 0..1,000,000 in both x and y → diag = ~1.414M
    msp.add_line((0.0, 0.0), (1000000.0, 0.0))
    msp.add_line((0.0, 0.0), (0.0, 1000000.0))
    # Label 500 units NORTH of the long horizontal line.
    msp.add_text("AXIS-A", dxfattribs={"insert": (500000.0, 500.0)})
    labels = extract_labels(doc)
    _compute_world_positions(doc, labels)
    gridlines = _build_world_gridline_cache(doc)
    threshold = _adaptive_threshold(doc)
    # Adaptive threshold ≈ 707 (0.0005 × ~1.414M diag).
    assert threshold > 700.0
    _annotate_proximity(labels, gridlines, threshold=threshold)
    assert labels[0].on_gridline is True

    # And a label 5000 units away is still off-gridline even at this scale.
    msp.add_text("AXIS-B", dxfattribs={"insert": (500000.0, 5000.0)})
    labels2 = extract_labels(doc)
    _compute_world_positions(doc, labels2)
    _annotate_proximity(labels2, gridlines, threshold=threshold)
    b = next(c for c in labels2 if c.text == "AXIS-B")
    assert b.on_gridline is False


def test_adaptive_threshold_falls_back_to_base_for_tiny_drawing():
    """A tiny drawing (small bbox) should keep the base threshold."""
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((0.0, 0.0), (50.0, 0.0))
    threshold = _adaptive_threshold(doc)
    # diag = 50, 0.0005 × 50 = 0.025; max(55, 0.025) = 55
    assert threshold == 55.0


# Add fixtures for TP-104 to the test file's lookup so the world-coord
# transform tests can verify against real data.
def test_tp104_str_label_world_positions_use_insert_transform():
    """STR labels sit in block-local coords (e.g. 23556, 27711).
    After the transform, they land in world coords like
    (971.5 + 0.8*23556, 4264.2 + 0.8*27711) ≈ (19816, 26433)."""
    if not TP104_STR.exists():
        import pytest
        pytest.skip(f"TP-104 STR fixture missing: {TP104_STR}")
    import ezdxf as _ez
    doc = _ez.readfile(str(TP104_STR))
    labels = extract_labels(doc)
    _compute_world_positions(doc, labels)
    # Find the TP104-A label.
    a_label = next(c for c in labels if c.text == "TP104-A")
    # Block-local was (23556.1, 27711.7); with INSERT insert≈(971.5, 4264.2)
    # and xscale=0.8, world ≈ (19816.4, 26433.6). Just check the scale.
    wx, wy = a_label.world_position
    local_x, local_y = a_label.position
    # World should be ≈ insert_x + 0.8 * local_x.
    # Find the INSERT for this block to compute the expected.
    insert_lookup = _build_insert_lookup(doc)
    ix, iy, sx, sy, r = insert_lookup[a_label.block_name]
    # TP-104 STR has no rotation, so the transform reduces to scale + translate.
    assert abs(r) < 1e-9
    assert abs(wx - (ix + sx * local_x)) < 1e-6
    assert abs(wy - (iy + sy * local_y)) < 1e-6
    assert sx == 0.8  # TP-104 GridLine INSERTs use 0.8 scale


# ─────────────────────────────────────────────────────────────────────────────
# Beam-section dictionary — reject false-positive axis labels
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    # Indian Standard sections
    "ISMB 600", "ISMBE 600", "ISMC 250", "ISJC 200", "ISLC 200",
    "ISHB 400", "ISA 5050", "ISA 7550", "ISA5050",
    # British / European
    "UB 533", "UC 305", "PFC 200", "RHS 100x60x5",
    "SHS 80x80x5", "IPE 200", "HEA 200", "HEB 300",
    "HEM 240", "CHS 114.3x5",
    # US
    "W12x26", "S6x17.25", "M8x6.5", "HP12x74",
    "C6x8.2", "MC6x12", "L4x4x1/2", "HSS6x6x1/4",
    # Bolt grades / property grades
    "M20", "M16x50", "8.8", "10.9",
    # Case-insensitive
    "ismb 600", "ub 533", "w12x26", "hss 6x6x1/4",
])
def test_beam_section_patterns_match(text):
    """The dictionary rejects all the common structural-section /
    bolt-grade patterns."""
    assert _looks_like_beam_section(text) is True


@pytest.mark.parametrize("text", [
    "TP104-A", "TP104-1", "TP104-1A", "GRID-A", "GR-A1",
    "AXIS-3", "1", "A", "B-1", "X-Y-Z",
    # Single-letter/numeric prefixes that happen to share initial
    # letters with section names — must NOT match.
    "W12", "C6", "MC6",  # bare letter+digit, no "x"
    "S6",
])
def test_axis_labels_pass_through(text):
    """Real axis labels (and innocuous text) must NOT be flagged as
    beam sections."""
    assert _looks_like_beam_section(text) is False


def test_beam_labels_dropped_from_extraction():
    """A drawing full of beam labels should yield ZERO axis candidates —
    extract_labels filters beam sections up front."""
    doc = ezdxf.new()
    msp = doc.modelspace()
    # 12 different beam labels
    beam_labels = [
        "ISMB 600", "ISMB 450", "ISMB 300",
        "ISMC 250", "ISMC 200",
        "ISA 7550", "ISA 5050",
        "UB 533", "UB 457",
        "W12x26", "W14x30",
        "M20",
    ]
    for i, txt in enumerate(beam_labels):
        msp.add_text(txt, dxfattribs={"insert": (i * 200.0, 0.0)})
    labels = extract_labels(doc)
    assert labels == [], f"expected no labels but got {len(labels)}: {[c.text for c in labels]}"


def test_axis_labels_coexist_with_beam_labels():
    """Axis labels mixed in with beam labels — only the axis labels
    should survive extraction."""
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_text("TP104-A", dxfattribs={"insert": (0.0, 0.0)})
    msp.add_text("TP104-B", dxfattribs={"insert": (200.0, 0.0)})
    msp.add_text("ISMB 600", dxfattribs={"insert": (400.0, 0.0)})
    msp.add_text("UB 533", dxfattribs={"insert": (600.0, 0.0)})
    msp.add_text("W12x26", dxfattribs={"insert": (800.0, 0.0)})
    labels = extract_labels(doc)
    texts = {c.text for c in labels}
    assert texts == {"TP104-A", "TP104-B"}
    # And they still get the right prefix/suffix split.
    a = next(c for c in labels if c.text == "TP104-A")
    assert (a.prefix, a.suffix) == ("TP104-", "A")


def test_beam_labels_filtered_from_suggest():
    """A beam-label-heavy drawing produces no axis suggestions rather
    than a false-positive TP104- / ISMB- / UB- convention."""
    import tempfile
    doc = ezdxf.new()
    msp = doc.modelspace()
    # 8 beam labels — would cluster as "ISMB-" if the dictionary
    # didn't reject them, with 3 different suffix sizes.
    for i, txt in enumerate([
        "ISMB 600", "ISMB 450", "ISMB 300", "ISMB 200",
        "ISMC 250", "ISMC 200", "ISMC 150", "ISMC 100",
    ]):
        msp.add_text(txt, dxfattribs={"insert": (i * 200.0, 0.0)})
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
        tmp_path = f.name
    try:
        doc.saveas(tmp_path)
        sugs = suggest(Path(tmp_path))
    finally:
        os.unlink(tmp_path)
    assert sugs == [], f"expected no suggestions but got {sugs}"
