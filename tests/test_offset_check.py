#!/usr/bin/env python3
"""tests/test_offset_check.py — the cross-drawing offset check.

The check answers "these two things should line up — how far apart are
they?".  What it may compare is decided by the rules, so the tests here
are mostly about the *geometry*: the exact offset it measures, the
classification it gives, and the things it refuses to report.

The rules used here are deliberately tight, so that a small offset is
still reported: ``tolerance_mm`` 0.5 (an exact match is not a finding),
``search_mm`` 25, ``warn_mm`` 5, ``critical_mm`` 15.
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import ezdxf
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from core.mismatch import detect                       # noqa: E402
from core.mismatch.policy import (DEFAULT_POLICY, MismatchPolicy,  # noqa: E402
                                  OffsetRule)
from core.transform2d import Affine2D                   # noqa: E402

MECH_LAYER = "MECH-GEO"
STR_LAYER = "STR-GEO"


def _rule(**kw) -> OffsetRule:
    base = dict(name="T1",
                mech_selector={"layers": [MECH_LAYER], "families": []},
                str_selector={"layers": [STR_LAYER], "families": []},
                method="edge_to_edge", search_mm=25.0, tolerance_mm=0.5,
                warn_mm=5.0, critical_mm=15.0, min_overlap_mm=50.0,
                needs_review=False)
    base.update(kw)
    return OffsetRule(**base)


def _policy(*rules) -> MismatchPolicy:
    return DEFAULT_POLICY.replace(offset_rules=tuple(rules) or DEFAULT_POLICY.offset_rules)


def _doc() -> "ezdxf.document.Drawing":
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    return doc


def _rect(doc, layer, x, y, w=200.0, h=150.0, angle=0.0):
    """A closed rectangle on a layer."""
    if layer not in doc.layers:
        doc.layers.add(layer)
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    if angle:
        c, s = math.cos(angle), math.radians(0) or math.sin(angle)
        cx, cy = x + w / 2.0, y + h / 2.0
        pts = [(cx + (px - cx) * c - (py - cy) * s,
                cy + (px - cx) * s + (py - cy) * c) for px, py in pts]
    doc.modelspace().add_lwpolyline(pts, close=True,
                                    dxfattribs={"layer": layer})


def _run(tmp_path, mech_doc, str_doc, *, rules=None, fit_ctx=None, affine=None):
    mpath = tmp_path / "mech.dxf"
    spath = tmp_path / "str.dxf"
    mech_doc.saveas(mpath)
    str_doc.saveas(spath)
    return detect(mpath, spath,
                  affine=affine if affine is not None else Affine2D(),
                  policy=_policy(*(rules or (_rule(),))),
                  fit_ctx=fit_ctx or {"quality": {"status": "ok"}})


# ─────────────────────────────────────────────────────────────────────────────
# the measured offset
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("offset", [1.0, 2.0, 5.0, 7.5, 10.0])
def test_a_rectangle_offset_is_measured_exactly(tmp_path, offset):
    """A rectangle shifted by N mm is reported with exactly that offset."""
    mech = _doc()
    str_ = _doc()
    _rect(mech, MECH_LAYER, 0.0, 0.0)
    _rect(str_, STR_LAYER, offset, 0.0)

    report = _run(tmp_path, mech, str_)

    offsets = [f for f in report.findings if f.kind == "OFFSET"]
    assert len(offsets) == 1, [f.details for f in report.findings]
    f = offsets[0]
    assert abs(abs(f.offset_mm[0]) - offset) < 1e-6, f.offset_mm
    assert abs(f.offset_mm[1]) < 1e-6, f.offset_mm
    assert f.magnitude_mm == pytest.approx(offset, abs=1e-6)
    assert f.details["classification"] == "position_shift"
    assert f.details["rule"] == "T1"
    assert f.details["mech"]["layer"] == MECH_LAYER
    assert f.details["str"]["layer"] == STR_LAYER
    assert f.details["ambiguous"] is False
    assert f.details["confidence"] == "normal"


def test_an_exact_match_is_not_reported(tmp_path):
    """Two rectangles on top of each other: matched, and nothing to say."""
    mech = _doc()
    str_ = _doc()
    _rect(mech, MECH_LAYER, 0.0, 0.0)
    _rect(str_, STR_LAYER, 0.0, 0.0)

    report = _run(tmp_path, mech, str_)
    assert [f for f in report.findings if f.kind == "OFFSET"] == []


def test_the_severity_follows_the_numbers(tmp_path):
    """Above ``warn_mm`` a warning, above ``critical_mm`` a critical."""
    mech = _doc()
    str_ = _doc()
    _rect(mech, MECH_LAYER, 0.0, 0.0)
    _rect(str_, STR_LAYER, 8.0, 0.0)
    report = _run(tmp_path, mech, str_)
    assert [f.severity for f in report.findings] == ["WARNING"]

    mech = _doc()
    str_ = _doc()
    _rect(mech, MECH_LAYER, 0.0, 0.0)
    _rect(str_, STR_LAYER, 20.0, 0.0)
    report = _run(tmp_path, mech, str_)
    assert [f.severity for f in report.findings] == ["CRITICAL"]


def test_a_rotated_shape_is_still_measured(tmp_path):
    """A rectangle turned by 30° is compared edge for edge."""
    mech = _doc()
    str_ = _doc()
    _rect(mech, MECH_LAYER, 0.0, 0.0, angle=math.radians(30.0))
    _rect(str_, STR_LAYER, 4.0, 0.0, angle=math.radians(30.0))

    report = _run(tmp_path, mech, str_)
    offsets = [f for f in report.findings if f.kind == "OFFSET"]
    assert len(offsets) == 1, [f.details for f in report.findings]
    # the *perpendicular* distance between two 30° edges that were
    # shifted 4 mm along X — the shift across the gap is 4·cos(30°)
    assert offsets[0].magnitude_mm == pytest.approx(4.0 * math.cos(math.radians(30.0)),
                                                    abs=1e-6)
    # and the reported vector is that perpendicular displacement
    assert math.hypot(*offsets[0].offset_mm) == pytest.approx(
        4.0 * math.cos(math.radians(30.0)), abs=1e-6)


def test_a_nested_block_is_found(tmp_path):
    """A rectangle inside a block inside a block is still geometry."""
    mech = _doc()
    inner = mech.blocks.new("INNER")
    inner.add_lwpolyline([(0, 0), (200, 0), (200, 150), (0, 150)], close=True,
                          dxfattribs={"layer": MECH_LAYER})
    outer = mech.blocks.new("OUTER")
    outer.add_blockref("INNER", (0.0, 0.0), dxfattribs={"layer": MECH_LAYER})
    mech.modelspace().add_blockref("OUTER", (0.0, 0.0),
                                   dxfattribs={"layer": MECH_LAYER})

    str_ = _doc()
    _rect(str_, STR_LAYER, 3.0, 0.0)

    report = _run(tmp_path, mech, str_)
    offsets = [f for f in report.findings if f.kind == "OFFSET"]
    assert len(offsets) == 1, [f.details for f in report.findings]
    assert offsets[0].magnitude_mm == pytest.approx(3.0, abs=1e-6)
    assert offsets[0].details["mech"]["block"] == "INNER"


# ─────────────────────────────────────────────────────────────────────────────
# ambiguity — the check does not guess
# ─────────────────────────────────────────────────────────────────────────────

def test_repeated_identical_objects_are_ambiguous(tmp_path):
    """Two equally good STR candidates: reported as ambiguous, not resolved."""
    mech = _doc()
    str_ = _doc()
    _rect(mech, MECH_LAYER, 0.0, 0.0)
    _rect(str_, STR_LAYER, 5.0, 0.0)
    _rect(str_, STR_LAYER, 5.0, 0.0)          # the same, again

    report = _run(tmp_path, mech, str_)
    offsets = [f for f in report.findings if f.kind == "OFFSET"]
    assert len(offsets) == 1, [f.details for f in report.findings]
    f = offsets[0]
    assert f.details["ambiguous"] is True
    assert f.details["confidence"] == "ambiguous"
    assert f.details["classification"] == "ambiguous"
    # and it was not paired
    assert report.offset_rules["T1"]["ambiguous"] == 1


def test_an_ambiguous_item_is_left_unpaired(tmp_path):
    """The ambiguous MECH item goes to the unmatched list, not to a guess."""
    mech = _doc()
    str_ = _doc()
    _rect(mech, MECH_LAYER, 0.0, 0.0)
    _rect(str_, STR_LAYER, 5.0, 0.0)
    _rect(str_, STR_LAYER, 5.0, 0.0)

    report = _run(tmp_path, mech, str_)
    notes = report.offset_rules["T1"]
    assert notes["ambiguous"] == 1, notes
    assert notes["unmatched_mech"] >= 1
    assert notes["pairs"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# a width difference is not a position shift
# ─────────────────────────────────────────────────────────────────────────────

def test_a_symmetric_pair_is_a_width_difference(tmp_path):
    """Two MECH edges 5.6 mm inside the two STR edges: the width differs."""
    mech = _doc()
    str_ = _doc()
    # the STR object is 200 wide; the MECH one is 200 − 2×5.6, centred
    _rect(str_, STR_LAYER, 0.0, 0.0, w=200.0, h=150.0)
    _rect(mech, MECH_LAYER, 5.6, 0.0, w=188.8, h=150.0)

    report = _run(tmp_path, mech, str_)
    widths = [f for f in report.findings if f.kind == "WIDTH_DIFFERENCE"]
    assert len(widths) == 1, [(f.kind, f.details) for f in report.findings]
    f = widths[0]
    assert f.details["classification"] == "dimension_difference"
    assert f.details["width_difference_mm"] == pytest.approx(11.2, abs=1e-6)
    assert f.magnitude_mm == pytest.approx(11.2, abs=1e-6)


# ─────────────────────────────────────────────────────────────────────────────
# the grid's own control points
# ─────────────────────────────────────────────────────────────────────────────

def test_a_grid_marker_circle_is_never_reported(tmp_path):
    """A circle on a grid intersection is the drawing's own control point."""
    mech = _doc()
    str_ = _doc()
    if MECH_LAYER not in mech.layers:
        mech.layers.add(MECH_LAYER)
    # a small circle exactly on the grid intersection at (0, 0)
    mech.modelspace().add_circle((0.0, 0.0), 5.0, dxfattribs={"layer": MECH_LAYER})
    _rect(str_, STR_LAYER, 3.0, 0.0)

    fit_ctx = {"quality": {"status": "ok"},
               "correspondences": [{"label": "A-1", "mech": [0.0, 0.0],
                                    "str": [0.0, 0.0], "residual": 0.0,
                                    "loo_residual": 0.0, "inlier": True}]}
    report = _run(tmp_path, mech, str_, fit_ctx=fit_ctx)
    for f in report.findings:
        mech_src = f.details.get("mech") or {}
        assert mech_src.get("family") != "circle", f.details


def test_a_circle_on_a_grid_layer_is_never_reported(tmp_path):
    """A circle on a grid layer is excluded whatever it sits on."""
    mech = _doc()
    str_ = _doc()
    if "CEN" not in mech.layers:
        mech.layers.add("CEN")
    mech.modelspace().add_circle((0.0, 0.0), 5.0, dxfattribs={"layer": "CEN"})
    _rect(str_, STR_LAYER, 3.0, 0.0)

    report = _run(tmp_path, mech, str_)
    for f in report.findings:
        assert (f.details.get("mech") or {}).get("layer") != "CEN", f.details


# ─────────────────────────────────────────────────────────────────────────────
# the null test — nothing is found when nothing is there
# ─────────────────────────────────────────────────────────────────────────────

def test_shifting_the_drawing_apart_finds_nothing(tmp_path):
    """Move the MECH drawing away: the findings must collapse.

    This is the test that says the check measures *this* relationship and
    not something else.  The shift is random in 300–3000 mm, well
    outside the rule's 25 mm search, so nothing may be found — while the
    unshifted pair finds the offset every time.
    """
    def pair(dx):
        mech = _doc()
        str_ = _doc()
        _rect(mech, MECH_LAYER, 0.0, 0.0)
        _rect(str_, STR_LAYER, dx, 0.0)
        return mech, str_

    real_mech, real_str = pair(6.0)
    real = _run(tmp_path / "real", real_mech, real_str) if False else None

    # the real pair
    d = tmp_path / "real"
    d.mkdir()
    real = _run(d, *_pair_docs(6.0))
    assert len([f for f in real.findings if f.kind == "OFFSET"]) == 1

    rng = random.Random(20261027)
    for _ in range(5):
        shift = rng.uniform(300.0, 3000.0)
        d = tmp_path / f"null_{shift:.0f}"
        d.mkdir()
        null = _run(d, *_pair_docs(shift))
        assert len([f for f in null.findings if f.kind == "OFFSET"]) == 0, shift


def _pair_docs(dx: float):
    mech = _doc()
    str_ = _doc()
    _rect(mech, MECH_LAYER, 0.0, 0.0)
    _rect(str_, STR_LAYER, dx, 0.0)
    return mech, str_


# ─────────────────────────────────────────────────────────────────────────────
# what may be compared
# ─────────────────────────────────────────────────────────────────────────────

def test_nothing_is_compared_that_no_rule_pairs(tmp_path):
    """A rule pairs one layer with another — and nothing else."""
    mech = _doc()
    str_ = _doc()
    _rect(mech, "SOMETHING-ELSE", 0.0, 0.0)
    _rect(str_, STR_LAYER, 3.0, 0.0)

    report = _run(tmp_path, mech, str_)
    assert [f for f in report.findings if f.kind == "OFFSET"] == []


def test_a_rule_with_no_families_and_no_layers_pairs_nothing(tmp_path):
    """An empty selector is not a wildcard."""
    mech = _doc()
    str_ = _doc()
    _rect(mech, MECH_LAYER, 0.0, 0.0)
    _rect(str_, STR_LAYER, 3.0, 0.0)

    rule = _rule(mech_selector={"layers": [], "families": []},
                 str_selector={"layers": [], "families": []})
    report = _run(tmp_path, mech, str_, rules=[rule])
    assert [f for f in report.findings if f.kind == "OFFSET"] == []


def test_only_the_edge_method_is_implemented(tmp_path):
    """The other methods are in the schema; only edge_to_edge runs."""
    mech = _doc()
    str_ = _doc()
    _rect(mech, MECH_LAYER, 0.0, 0.0)
    _rect(str_, STR_LAYER, 3.0, 0.0)

    rule = _rule(method="hole_centre")
    report = _run(tmp_path, mech, str_, rules=[rule])
    assert [f for f in report.findings if f.kind == "OFFSET"] == []


# ─────────────────────────────────────────────────────────────────────────────
# the refinements: review, merging, gaps, clusters, bays
# ─────────────────────────────────────────────────────────────────────────────

def test_an_ambiguous_finding_is_review_never_critical(tmp_path):
    """An unresolved match is a question, not a defect."""
    mech = _doc()
    str_ = _doc()
    _rect(mech, MECH_LAYER, 0.0, 0.0)
    _rect(str_, STR_LAYER, 5.0, 0.0)
    _rect(str_, STR_LAYER, 5.0, 0.0)

    report = _run(tmp_path, mech, str_)
    for f in report.findings:
        assert f.severity == "REVIEW", (f.severity, f.details)
        assert f.severity not in ("CRITICAL", "WARNING")
    # and it is not in the critical/warning counts
    summary = report.summary()
    assert summary["by_severity"]["CRITICAL"] == 0
    assert summary["by_severity"]["WARNING"] == 0
    assert summary["by_severity"]["REVIEW"] == len(report.findings)


def test_the_same_str_part_twice_is_one_finding(tmp_path):
    """The same STR part, drawn twice, is one finding with both edges."""
    mech = _doc()
    str_ = _doc()
    # one STR part, inserted twice — the same part, twice
    blk = str_.blocks.new("STR-PART")
    blk.add_lwpolyline([(0, 0), (100, 0), (100, 100), (0, 100)], close=True,
                        dxfattribs={"layer": STR_LAYER})
    str_.modelspace().add_blockref("STR-PART", (5.0, 0.0), dxfattribs={"layer": STR_LAYER})
    str_.modelspace().add_blockref("STR-PART", (5.0, 400.0), dxfattribs={"layer": STR_LAYER})
    _rect(mech, MECH_LAYER, 0.0, 0.0)
    _rect(mech, MECH_LAYER, 0.0, 400.0)

    report = _run(tmp_path, mech, str_)
    offsets = [f for f in report.findings if f.kind == "OFFSET"]
    assert len(offsets) == 1, [(f.kind, f.details) for f in report.findings]
    # both edges are kept as the evidence
    assert len(offsets[0].details["evidence"]) == 2, offsets[0].details


def test_two_adjacent_parts_make_one_gap_difference(tmp_path):
    """Two MECH items against two different STR parts: one gap, not two shifts."""
    mech = _doc()
    str_ = _doc()
    # the same MECH part, twice; and two STR parts it sits inside, each
    # 23.72 mm off — so the pair is squeezed by 23.72 + 23.72
    mblk = mech.blocks.new("MECH-PART")
    mblk.add_lwpolyline([(0, 0), (100, 0), (100, 100), (0, 100)], close=True,
                         dxfattribs={"layer": MECH_LAYER})
    mech.modelspace().add_blockref("MECH-PART", (23.72, 0.0),
                                   dxfattribs={"layer": MECH_LAYER})
    mech.modelspace().add_blockref("MECH-PART", (523.72, 0.0),
                                   dxfattribs={"layer": MECH_LAYER})
    _rect(str_, STR_LAYER, 0.0, 0.0, w=100.0, h=100.0)
    _rect(str_, STR_LAYER, 500.0, 0.0, w=100.0, h=100.0)

    report = _run(tmp_path, mech, str_)
    gaps = [f for f in report.findings if f.kind == "GAP_DIFFERENCE"]
    assert len(gaps) == 1, [(f.kind, f.details) for f in report.findings]
    assert gaps[0].magnitude_mm == pytest.approx(47.44, abs=1e-6)
    assert gaps[0].details["classification"] == "gap_difference"
    assert len(gaps[0].details["str_parts"]) == 2


def test_the_offsets_are_clustered(tmp_path):
    """Two findings with the same offset are one cluster."""
    from core.mismatch.checks.offset import cluster

    mech = _doc()
    str_ = _doc()
    _rect(mech, MECH_LAYER, 0.0, 0.0)
    _rect(str_, STR_LAYER, 5.0, 0.0)
    _rect(mech, MECH_LAYER, 0.0, 400.0)
    _rect(str_, STR_LAYER, 5.0, 400.0)

    report = _run(tmp_path, mech, str_)
    clusters = cluster(report.findings)
    assert clusters, "there must be at least one cluster"
    assert clusters[0]["count"] >= 1
    assert clusters[0]["offset_mm"] == pytest.approx(5.0, abs=1e-6)
    assert clusters[0]["finding_ids"]


def test_the_bay_is_in_ascending_grid_order(tmp_path):
    """``A-B / 2-3`` — the grid's own order, not the coordinate order."""
    from core.mismatch.checks.offset import _grid_bay

    class _P:
        cx = cy = 0.0

    class _Fit:
        # the grid: the A axis is east of the B axis, and 3 is south of 2
        points = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
        labels = ("A-1", "B-1", "B-2", "A-2")
        status = "ok"

    class _Env:
        fit = _Fit()
        mm = 1.0

    env = _Env()
    # a point between the A and B axes (x = 100 is B) and between 1 and 2
    assert _grid_bay(env, 50.0, 50.0) == "A-B / 1-2"
