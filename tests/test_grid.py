#!/usr/bin/env python3
"""test_grid.py — core/grid.py (PLAN.md step 4): world-coordinate grid detection."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import ezdxf
import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import synth  # noqa: E402
from core import grid  # noqa: E402

PREFIX = "TP104-"
SUFFIXES = ["A", "B", "C", "D", "1", "2", "3"]


def _extent(pts) -> float:
    a = np.asarray([[p[0], p[1]] for p in pts], dtype=float)
    return float(np.hypot(*np.ptp(a, axis=0)))


def _assert_axes_match(res, truth_axes, extent, unit=1.0):
    """Every detected axis line passes within 1e-6*extent of two truth points."""
    for lab, (pt, d) in truth_axes.items():
        ax = res.axes[lab]
        assert ax.status == "ok", (lab, ax.status, ax.reasons)
        n = np.array([-ax.dir[1], ax.dir[0]]) / math.hypot(ax.dir[0], ax.dir[1])
        for q in (pt, pt + d * (1000.0 * unit)):
            dist = abs(float(n @ (np.array([q[0], q[1]]) - np.array([ax.point[0], ax.point[1]]))))
            assert dist <= 1e-6 * extent, (lab, dist)


def _read(p):
    return ezdxf.readfile(str(p))


# ------------------------------------------------------------ walk_world

def test_walk_world_nested_rotated_mirrored_base_point():
    doc = ezdxf.new()
    inner = doc.blocks.new("INNER")
    inner.add_line((1, 2), (4, 3))
    outer = doc.blocks.new("OUTER")
    outer.block.dxf.base_point = (5, 5, 0)
    outer.add_line((6, 7), (9, 8))
    outer.add_blockref("INNER", (2, 1), dxfattribs={"xscale": -1.0, "rotation": 10.0})
    doc.modelspace().add_blockref("OUTER", (100, 200), dxfattribs={"rotation": 30.0})

    def rot(p, deg):
        c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        return (c * p[0] - s * p[1], s * p[0] + c * p[1])

    def outer_to_world(p):
        q = rot((p[0] - 5, p[1] - 5), 30.0)
        return (q[0] + 100, q[1] + 200)

    def inner_to_outer(p):
        q = rot((-p[0], p[1]), 10.0)           # xscale=-1 then rotate
        return (q[0] + 2, q[1] + 1)

    got = {}
    for it in grid.walk_world(doc):
        if it.entity.dxftype() == "LINE":
            s = grid._world_pt(it.m, it.entity.dxf.start)
            e = grid._world_pt(it.m, it.entity.dxf.end)
            got[it.chain] = ((s.x, s.y), (e.x, e.y))
    exp_outer = (outer_to_world((6, 7)), outer_to_world((9, 8)))
    exp_inner = (outer_to_world(inner_to_outer((1, 2))), outer_to_world(inner_to_outer((4, 3))))
    for key, exp in ((("OUTER",), exp_outer), (("OUTER", "INNER"), exp_inner)):
        for g, x in zip(got[key], exp):
            assert g[0] == pytest.approx(x[0], abs=1e-9)
            assert g[1] == pytest.approx(x[1], abs=1e-9)


def test_walk_world_cycle_guard():
    doc = ezdxf.new()
    a = doc.blocks.new("A")
    b = doc.blocks.new("B")
    a.add_blockref("B", (1, 0))
    b.add_blockref("A", (0, 1))
    b.add_line((0, 0), (1, 0))
    doc.modelspace().add_blockref("A", (0, 0))
    assert len([i for i in grid.walk_world(doc) if i.entity.dxftype() == "LINE"]) == 1


def test_attrib_world_position():
    doc = ezdxf.new()
    blk = doc.blocks.new("GL")
    blk.block.dxf.base_point = (10, 0, 0)
    blk.add_line((0, 0), (1000, 0))
    blk.add_attdef("NO", insert=(1100, 0), text="TP104-A", dxfattribs={"height": 100})
    ins = doc.modelspace().add_blockref("GL", (500, 600), dxfattribs={"rotation": 90.0})
    ins.add_auto_attribs({"NO": "TP104-A"})
    occ = grid.label_occurrences(doc, "TP104-", ["A"])["A"]
    assert len(occ) == 1 and occ[0].kind == "ATTRIB"
    # block (1100,0), base (10,0): local offset 1090 -> rotated 90 -> (0,1090) + insert
    assert occ[0].world_pt.x == pytest.approx(500.0, abs=1e-9)
    assert occ[0].world_pt.y == pytest.approx(600.0 + 1090.0, abs=1e-9)


def test_label_occurrences_all_kinds_no_dedup():
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_text("TP104-A", dxfattribs={"insert": (0, 0)})
    msp.add_text(" tp104-a ", dxfattribs={"insert": (5, 0)})
    msp.add_mtext("TP104-A", dxfattribs={"insert": (9, 0)})
    msp.add_text("TP104-AB", dxfattribs={"insert": (1, 0)})
    occ = grid.label_occurrences(doc, "TP104-", ["A", "B"])
    assert len(occ["A"]) == 3 and occ["B"] == []
    assert {o.kind for o in occ["A"]} == {"TEXT", "MTEXT"}


# ------------------------------------------------------------ detect_grid

def test_detect_grid_str_hard_matches_truth(fx_synth_hard):
    p = fx_synth_hard
    res = grid.detect_grid(_read(p.str_path), PREFIX, SUFFIXES)
    ext = _extent([v[0] for v in p.grid_str.values()])
    _assert_axes_match(res, p.grid_str, ext, unit=p.truth.scale ** -1 * 1.0 * p.truth.scale)


def test_detect_grid_mech_hard_matches_truth(fx_synth_hard):
    p = fx_synth_hard
    res = grid.detect_grid(_read(p.mech_path), PREFIX, SUFFIXES)
    _assert_axes_match(res, p.grid_mech, _extent([v[0] for v in p.grid_mech.values()]))
    assert res.axes["D"].n_occ == 2          # MTEXT label, both ends
    assert res.axes["D"].status == "ok"


def test_attrib_label_axis_detected(fx_synth_hard):
    res = grid.detect_grid(_read(fx_synth_hard.str_path), PREFIX, SUFFIXES)
    assert res.axes["3"].status == "ok"
    assert {o.kind for o in res.axes["3"].occurrences} == {"ATTRIB"}


def test_duplicate_labels_both_ends_vote_same_line(fx_synth_simple):
    res = grid.detect_grid(_read(fx_synth_simple.mech_path), PREFIX, SUFFIXES)
    for lab in SUFFIXES:
        ax = res.axes[lab]
        assert ax.n_occ == 2 and ax.n_agree == 2 and ax.status == "ok", (lab, ax.reasons)


def test_decoy_line_not_chosen(fx_synth_hard):
    """Decoy: a long horizontal CENTER line whose midpoint is nearer to the
    top label of the first vertical axis than the true line's midpoint."""
    p = fx_synth_hard
    for path, truth in ((p.mech_path, p.grid_mech), (p.str_path, p.grid_str)):
        res = grid.detect_grid(_read(path), PREFIX, SUFFIXES)
        first_vertical = min(k for k in ("A", "B", "C", "D"))
        ax = res.axes[first_vertical]
        d_true = truth[first_vertical][1]
        assert abs(ax.dir[0] * d_true[1] - ax.dir[1] * d_true[0]) < 1e-9   # parallel to truth
        # nothing is ambiguous / shared
        assert all(a.status == "ok" for a in res.axes.values())


def test_decoy_chosen_without_it_is_absent(tmp_path):
    """Control: with the decoy removed the same label still resolves."""
    pair = synth.build_pair(tmp_path, synth.with_opts(synth.LEGACY_COMPATIBLE, decoy_line=False))
    res = grid.detect_grid(_read(pair.mech_path), PREFIX, SUFFIXES)
    assert res.axes["A"].status == "ok"


def test_letters_on_y_axis(tmp_path):
    opts = synth.with_opts(synth.HARD, letters_on_y=True)
    p = synth.build_pair(tmp_path, opts)
    rm = grid.detect_grid(_read(p.mech_path), PREFIX, SUFFIXES)
    rs = grid.detect_grid(_read(p.str_path), PREFIX, SUFFIXES)
    _assert_axes_match(rm, p.grid_mech, _extent([v[0] for v in p.grid_mech.values()]))
    _assert_axes_match(rs, p.grid_str, _extent([v[0] for v in p.grid_str.values()]))
    # families are by direction, not by letter/digit
    assert rm.families == (["1", "2", "3"], ["A", "B", "C", "D"])
    assert abs(rm.axes["A"].dir[1]) < 1e-9            # letters are horizontal here
    assert abs(rm.axes["1"].dir[0]) < 1e-9


def test_m_units_thresholds(fx_synth_hard):
    """STR is in metres (extent ~20); no absolute-unit threshold may drop its lines."""
    p = fx_synth_hard
    assert p.opts.str_units == "m"
    res = grid.detect_grid(_read(p.str_path), PREFIX, SUFFIXES)
    assert res.extent < 100.0
    assert len(res.ok_axes()) == 7
    mech = grid.detect_grid(_read(p.mech_path), PREFIX, SUFFIXES)
    assert mech.extent > 1000.0 and len(mech.ok_axes()) == 7


def test_missing_label_reported_not_silent():
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((0, 0), (0, 1000))
    msp.add_line((500, 0), (500, 1000))
    msp.add_line((0, 0), (500, 0))
    msp.add_text("TP104-A", dxfattribs={"insert": (0, 1100)})
    msp.add_text("TP104-B", dxfattribs={"insert": (500, 1100)})
    res = grid.detect_grid(doc, "TP104-", ["A", "B", "C"])
    assert res.axes["C"].status == "missing"
    assert res.axes["C"].reasons and "not found" in res.axes["C"].reasons[0]
    assert res.axes["A"].status == "ok" and res.axes["B"].status == "ok"


def test_label_without_nearby_line_is_missing_with_reason():
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((0, 0), (0, 1000))
    msp.add_text("TP104-A", dxfattribs={"insert": (50000, 50000)})
    res = grid.detect_grid(doc, "TP104-", ["A"])
    assert res.axes["A"].status == "missing"
    # the reason must say *why* nothing was near — the exact wording is not
    # the contract (the axis-aligned fallback has its own message).
    reasons = " ".join(res.axes["A"].reasons)
    assert "no candidate" in reasons or "no axis-aligned line" in reasons, reasons


def test_tied_vote_is_ambiguous():
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((0, 0), (0, 1000))
    msp.add_line((300, 0), (300, 1000))
    msp.add_text("TP104-A", dxfattribs={"insert": (0, 1050)})
    msp.add_text("TP104-A", dxfattribs={"insert": (300, 1050)})
    res = grid.detect_grid(doc, "TP104-", ["A"])
    assert res.axes["A"].status == "ambiguous" and res.axes["A"].reasons


def test_merge_collinear_dashed():
    from ezdxf.math import Vec2
    segs = [grid.Segment(Vec2(x, 0), Vec2(x + 80, 0), "L", (), ()) for x in range(0, 1000, 100)]
    cands = grid.merge_collinear(segs, 1e-6, 1e-6, gap_tol=50)
    assert len(cands) == 1 and cands[0].length == pytest.approx(980.0)


# ------------------------------------------------------------ intersections

@pytest.mark.parametrize("which", ["mech", "str"])
def test_intersections_match_truth(fx_synth_hard, which):
    p = fx_synth_hard
    path = p.mech_path if which == "mech" else p.str_path
    truth = p.intersections_mech if which == "mech" else p.intersections_str
    res = grid.detect_grid(_read(path), PREFIX, SUFFIXES)
    inter = grid.intersections(res)
    assert len(inter) == 12
    by_set = {frozenset(k): v for k, v in inter.items()}
    ext = _extent(list(truth.values()))
    for k, v in truth.items():
        got = by_set[frozenset(k)]
        assert math.hypot(got.x - v[0], got.y - v[1]) <= 1e-6 * ext, k


def test_intersection_keys_ordered_by_family(fx_synth_hard):
    res = grid.detect_grid(_read(fx_synth_hard.mech_path), PREFIX, SUFFIXES)
    f0, f1 = res.families
    assert f0 == ["1", "2", "3"] and f1 == ["A", "B", "C", "D"]   # "1" sorts before "A"
    assert all(a in f0 and b in f1 for a, b in grid.intersections(res))
