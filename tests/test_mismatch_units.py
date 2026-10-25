#!/usr/bin/env python3
"""test_mismatch_units.py — unit tests for steps 8b.2 and 8b.3.

Step 8b.2:

``core/spatial.py``
    ``GridIndex`` against brute force, including the ``large``-item path
``core/mismatch/model.py``
    the deterministic ranking, the content-hash ``uid`` and the CSV shape
``core/mismatch/policy.py``
    strict ``from_dict``, regex validation, precedence and ``policy_hash``

Step 8b.3 (the last section of this file) covers the §6.2 extraction
tests — circles from bulges and nuts, OCS, non-uniform scale, rects,
bands, SAT, vertex-outside distance and layer-0 inheritance — against
hand-built drawings, plus ``walk_world(root=…)``.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np
import pytest

from core.mismatch import model as M
from core.mismatch.policy import (
    DEFAULT_POLICY,
    MismatchPolicy,
    PolicyError,
    load_policy,
    policy_hash,
)
from core.spatial import GridIndex

HERE = Path(__file__).resolve().parent
SUFFIXES = ["A", "B", "C", "D", "1", "2", "3"]


# ─────────────────────────────────────────────────────────────────────────────
# core/spatial.py
# ─────────────────────────────────────────────────────────────────────────────

def _brute_bbox(boxes, q):
    qx0, qy0, qx1, qy1 = q
    return np.flatnonzero((boxes[:, 0] <= qx1) & (boxes[:, 2] >= qx0)
                          & (boxes[:, 1] <= qy1) & (boxes[:, 3] >= qy0)).astype(np.int64)


def _brute_radius(boxes, xy, r):
    x, y = xy
    dx = np.maximum(np.maximum(boxes[:, 0] - x, x - boxes[:, 2]), 0.0)
    dy = np.maximum(np.maximum(boxes[:, 1] - y, y - boxes[:, 3]), 0.0)
    return np.flatnonzero(np.hypot(dx, dy) <= r).astype(np.int64)


def _random_boxes(n, seed):
    rng = np.random.default_rng(seed)
    lo = rng.uniform(-5000, 5000, size=(n, 2))
    size = rng.uniform(0.5, 300.0, size=(n, 2))
    return np.column_stack([lo, lo + size])


@pytest.mark.parametrize("cell", [None, 5.0, 137.0, 100000.0])
def test_gridindex_matches_bruteforce(cell):
    """query_bbox / query_radius / knn agree with an O(N) scan, every cell size."""
    boxes = _random_boxes(400, seed=20240501)
    gi = GridIndex(boxes, cell)
    rng = np.random.default_rng(7)
    for _ in range(60):
        cx, cy = rng.uniform(-6000, 6000, 2)
        w, h = rng.uniform(1.0, 900.0, 2)
        q = (cx - w, cy - h, cx + w, cy + h)
        got = gi.query_bbox(q)
        assert np.array_equal(got, _brute_bbox(boxes, q))
        assert np.array_equal(np.unique(got), got), "results must be sorted + unique"

        r = float(rng.uniform(0.0, 800.0))
        assert np.array_equal(gi.query_radius((cx, cy), r),
                              _brute_radius(boxes, (cx, cy), r))

        got_knn = gi.knn((cx, cy), 7, 1000.0)
        centres = np.column_stack([(boxes[:, 0] + boxes[:, 2]) / 2,
                                   (boxes[:, 1] + boxes[:, 3]) / 2])
        d = np.hypot(centres[:, 0] - cx, centres[:, 1] - cy)
        cand = _brute_radius(boxes, (cx, cy), 1000.0)
        want = cand[np.lexsort((cand, d[cand]))][:7]
        assert np.array_equal(got_knn, want)


def test_gridindex_large_items_are_always_returned():
    """An item spanning > 64 cells goes to `large` and must never be lost."""
    boxes = np.vstack([_random_boxes(300, seed=3),
                       [[-90000.0, -90000.0, 90000.0, 90000.0]]])   # 301 items
    # cell=100 keeps every 300 mm-wide box inside 3x3 buckets, so only the
    # giant one (1800x1800 cells) overflows
    gi = GridIndex(boxes, cell=100.0)
    assert gi.large.tolist() == [300]
    assert gi.query_bbox((0.0, 0.0, 1.0, 1.0)).tolist() == [300]
    assert gi.query_radius((89999.0, 89999.0), 10.0).tolist() == [300]
    assert 300 in gi.knn((-90000.0, -90000.0), 1, 1.0).tolist()
    assert np.array_equal(gi.query_bbox((-1e6, -1e6, 1e6, 1e6)),
                          np.arange(301, dtype=np.int64))


def test_gridindex_is_deterministic_across_repeats():
    boxes = _random_boxes(250, seed=99)
    a = GridIndex(boxes)
    b = GridIndex(boxes)
    for _ in range(5):
        assert np.array_equal(a.query_bbox((0, 0, 500, 500)), b.query_bbox((0, 0, 500, 500)))
        assert np.array_equal(a.knn((12.5, -7.5), 10, 900.0),
                              b.knn((12.5, -7.5), 10, 900.0))


def test_gridindex_empty_and_malformed():
    gi = GridIndex(np.zeros((0, 4)))
    assert len(gi) == 0
    assert gi.query_bbox((0, 0, 1, 1)).size == 0
    assert gi.query_radius((0, 0), 5).size == 0
    assert gi.knn((0, 0), 3, 5).size == 0
    assert GridIndex.auto_cell(np.zeros((0, 4))) == 1.0

    with pytest.raises(ValueError):
        GridIndex(np.zeros((5, 3)))
    with pytest.raises(ValueError):
        GridIndex(np.array([[0.0, 0.0, np.nan, 1.0]]))
    with pytest.raises(ValueError):
        GridIndex(_random_boxes(5, 1)).query_bbox((0, 0, 1))


def test_gridindex_auto_cell_scales_with_density():
    for n in (1, 10, 1000, 20000):
        boxes = _random_boxes(n, seed=n)
        cell = GridIndex.auto_cell(boxes)
        assert 0 < cell <= 20000.0
        # every item must be findable regardless of how it was bucketed
        gi = GridIndex(boxes)
        assert gi.query_radius((0.0, 0.0), 1e9).tolist() == list(range(n))


# ─────────────────────────────────────────────────────────────────────────────
# core/mismatch/policy.py
# ─────────────────────────────────────────────────────────────────────────────

def test_policy_default_roundtrip():
    d = DEFAULT_POLICY.to_dict()
    assert MismatchPolicy.from_dict(d) == DEFAULT_POLICY
    assert MismatchPolicy.from_dict(json.loads(json.dumps(d))) == DEFAULT_POLICY
    assert set(d["families"]) == set(DEFAULT_POLICY.family_names())
    assert len(d["families"]) == 12


@pytest.mark.parametrize("bad,needle", [
    ({"tol_mm": {"suport": 10}}, "suport"),
    ({"max_findings": 10, "wat": True}, "wat"),
    ({"checks": {"bolts": True, "extra_holee": False}}, "extra_holee"),
    ({"pairs": {"bolts": [["mech_bolt", "str_hole"]], "platefallback": "str_plate"}},
     "platefallback"),
    ({"families": {"mech_bolt": {"src": "MECH", "feature": "circle",
                                 "layer_regex": "bolt", "block_regex": None,
                                 "size_key": "z"}}}, "size_key"),
    ({"families": {"mech_bolt": {"src": "MECH", "feature": "circle",
                                 "layer_regex": "bolt", "block_regex": None,
                                 "extra": 1}}}, "extra"),
])
def test_policy_unknown_key_raises(bad, needle):
    """Any unknown key, top level or nested, is a hard error."""
    doc = DEFAULT_POLICY.to_dict()
    doc.update(bad)
    with pytest.raises(PolicyError) as exc:
        MismatchPolicy.from_dict(doc)
    assert needle in str(exc.value)


@pytest.mark.parametrize("doc", [
    {"families": {"mech_bolt": {"src": "MECH", "feature": "circle",
                                "layer_regex": "bolt[", "block_regex": None}}},
    {"families": {"mech_bolt": {"src": "MECH", "feature": "circle",
                                "layer_regex": "bolt", "block_regex": "*(bad"}}},
])
def test_policy_bad_regex_raises(doc):
    with pytest.raises(PolicyError) as exc:
        MismatchPolicy.from_dict(doc)
    assert "regular expression" in str(exc.value)


def test_policy_bad_values_raise():
    doc = DEFAULT_POLICY.to_dict()
    doc["source_docs"] = "cleaned_files"
    with pytest.raises(PolicyError):
        MismatchPolicy.from_dict(doc)

    doc = DEFAULT_POLICY.to_dict()
    doc["families"]["str_plate"]["size_min_mm"] = "wide"
    with pytest.raises(PolicyError):
        MismatchPolicy.from_dict(doc)

    doc = DEFAULT_POLICY.to_dict()
    doc["pairs"]["containment"] = [["mech_skid", "str_no_such_family"]]
    with pytest.raises(PolicyError) as exc:
        MismatchPolicy.from_dict(doc)
    assert "unknown family" in str(exc.value)


def test_policy_hash_stable():
    a = policy_hash(DEFAULT_POLICY)
    assert a == DEFAULT_POLICY.policy_hash
    # key order in the source document must not matter
    reordered = dict(reversed(list(DEFAULT_POLICY.to_dict().items())))
    assert policy_hash(MismatchPolicy.from_dict(reordered)) == a
    # ... but content must
    for tweak in ({"max_findings": 4999},
                  {"checks": dict(DEFAULT_POLICY.checks.to_dict(), extra_hole=True)},
                  {"crit_bearing": 0.4},
                  {"families": dict(DEFAULT_POLICY.to_dict()["families"],
                                    mech_bolt=dict(DEFAULT_POLICY.require_family(
                                        "mech_bolt").to_dict(), size_max_mm=41.0))}):
        doc = DEFAULT_POLICY.to_dict()
        doc.update(tweak)
        assert policy_hash(MismatchPolicy.from_dict(doc)) != a, tweak


def test_policy_precedence_job_over_default(fx_isolated_jobs, tmp_path):
    """DEFAULT < jobs/<id>/mismatch_policy.json < <pair_root>/… < options."""
    from jobs import job_dir

    job_id = "policytest"
    (job_dir(job_id)).mkdir(parents=True, exist_ok=True)
    (job_dir(job_id) / "mismatch_policy.json").write_text(json.dumps({
        "checks": {"bolts": False},
        "max_findings": 11,
    }))
    pair_root = tmp_path / "pair"
    pair_root.mkdir()
    (pair_root / "mismatch_policy.json").write_text(json.dumps({"max_findings": 22}))

    p = load_policy(job_id=job_id)
    assert p.checks.bolts is False and p.max_findings == 11
    assert p.families == DEFAULT_POLICY.families      # untouched section survives

    p = load_policy(job_id=job_id, pair_root=pair_root)
    assert p.max_findings == 22                       # pair file wins over the job file
    assert p.checks.bolts is False

    p = load_policy(job_id=job_id, pair_root=pair_root,
                    options={"mismatch": {"max_findings": 33}})
    assert p.max_findings == 33                       # options win over everything
    assert p.checks.bolts is False

    # no job at all -> the defaults, unchanged
    assert load_policy().to_dict() == DEFAULT_POLICY.to_dict()
    assert load_policy(job_id="does-not-exist").max_findings == 5000


def test_policy_file_must_be_valid_json(fx_isolated_jobs):
    from jobs import job_dir

    job_id = "badpolicy"
    (job_dir(job_id)).mkdir(parents=True, exist_ok=True)
    (job_dir(job_id) / "mismatch_policy.json").write_text("{not json")
    with pytest.raises(PolicyError):
        load_policy(job_id=job_id)


# ─────────────────────────────────────────────────────────────────────────────
# core/mismatch/model.py
# ─────────────────────────────────────────────────────────────────────────────

def _ref(source, handle, chain=("ROOT",), layer="L", family="mech_bolt"):
    return M.EntityRef(source=source, handle=handle, insert_chain=tuple(chain),
                       block=chain[-1] if chain else "", root_block=chain[0] if chain else "",
                       layer=layer, family=family, feature="circle", size_mm={"r": 10.0})


def _finding(kind="CLASH", severity="WARNING", ratio=None, xy=(1.0, 2.0),
             mech="AB", st="CD", origin="mismatch"):
    return M.Finding(kind=kind, severity=severity, origin=origin, ratio=ratio,
                     location=xy, magnitude_mm=3.0, tolerance_mm=1.0,
                     tolerance_eff_mm=1.0,
                     mech_ref=_ref("MECH", mech) if mech else None,
                     str_ref=_ref("STR", st) if st else None)


def test_finding_uid_is_content_hash_and_stable():
    a = _finding()
    b = _finding()
    assert a.compute_uid() == b.compute_uid()
    assert len(a.compute_uid()) == 12
    assert a.compute_uid() == M.finding_uid(a.kind, a.mech_ref, a.str_ref)
    # any change to kind / either ref changes it
    assert a.compute_uid() != _finding(kind="CLASH", st="CE").compute_uid()
    assert a.compute_uid() != _finding(mech="AC").compute_uid()
    assert a.compute_uid() != _finding(kind="CLEARANCE").compute_uid()
    # a one-sided finding still gets a uid
    assert len(_finding(mech="AB", st=None).compute_uid()) == 12


def test_rank_findings_is_deterministic_and_severity_first():
    fs = [
        _finding("EDGE_DISTANCE", "WARNING", 0.5, (10.0, 0.0), "A", "B"),
        _finding("CLASH", "CRITICAL", 1.0, (5.0, 5.0), "C", "D"),
        _finding("EXTRA_HOLE", "INFO", None, (1.0, 1.0), "E", "F"),
        _finding("CLASH", "CRITICAL", 4.0, (2.0, 2.0), "G", "H"),
        _finding("CLASH", "CRITICAL", 1.0, (1.0, 0.0), "I", "J"),
        _finding("SUPPORT_OFF_MEMBER", "WARNING", 9.0, (0.0, 0.0), "K", "L"),
    ]
    ranked = M.rank_findings(fs)
    assert [f.id for f in ranked] == ["MM-0001", "MM-0002", "MM-0003",
                                      "MM-0004", "MM-0005", "MM-0006"]
    assert [f.severity for f in ranked] == ["CRITICAL"] * 3 + ["WARNING"] * 2 + ["INFO"]
    assert [f.kind for f in ranked][:2] == ["CLASH", "CLASH"]      # ratio 4.0 before 1.0
    assert all(f.uid for f in ranked)

    # shuffling the input must not change the order or the ids
    rng = np.random.default_rng(4)
    for _ in range(20):
        idx = rng.permutation(len(fs))
        again = M.rank_findings([fs[int(i)] for i in idx])
        assert [f.id for f in again] == [f.id for f in ranked]
        assert [f.uid for f in again] == [f.uid for f in ranked]

    # spotcheck-origin findings keep the id they were promoted with
    spot = _finding(kind="SPOT_NOBEAM", severity="CRITICAL", origin="spotcheck")
    spot.id = "ISSUE-0007"
    out = M.rank_findings([spot] + fs)
    assert spot.id == "ISSUE-0007"
    assert [f.id for f in out if f.origin == "mismatch"] == [
        f"MM-{i:04d}" for i in range(1, 7)]


def test_rank_key_orders_ratio_none_last():
    a = _finding(ratio=0.1)
    b = _finding(ratio=None)
    assert M.rank_key(a) < M.rank_key(b)


def test_truncate_keeps_the_most_severe():
    fs = M.rank_findings([_finding("CLASH", "CRITICAL", 1.0 / (i + 1), (float(i), 0.0))
                          for i in range(10)])
    kept, was_truncated = M.truncate(fs, 3)
    assert was_truncated and len(kept) == 3
    assert [f.id for f in kept] == ["MM-0001", "MM-0002", "MM-0003"]
    kept, was_truncated = M.truncate(fs, 10)
    assert not was_truncated and len(kept) == 10
    assert M.truncate(fs, 0)[1] is False


def test_csv_columns_and_row():
    f = _finding("BOLT_HOLE_OFFSET", "CRITICAL", 4.0, (123.456, 7.0), "AB", "CD")
    f.id, f.uid = "MM-0001", "deadbeefcafe"
    f.offset_mm = (-4.1, 1.2)
    f.local_offset_mm = (-3.9, 1.0)
    f.local_magnitude_mm = 4.02
    f.str_location = (119.3, 8.2)
    f.rotation_deg = 0.0
    f.message = "hello"
    row = M.finding_row(f)
    assert tuple(row) == M.CSV_COLUMNS
    assert row["x"] == 123.456 and row["str_y"] == 8.2
    assert row["dx_mm"] == -4.1 and row["mech_layer"] == "L"
    assert row["systematic"] is False and row["within_fit_uncertainty"] is False


def test_write_csv_roundtrip(tmp_path):
    fs = M.rank_findings([_finding(kind=k, severity=s, ratio=r, xy=(i, i))
                          for i, (k, s, r) in enumerate(
                              [("CLASH", "CRITICAL", 2.0), ("MISSING_HOLE", "INFO", None)])])
    p = M.write_csv(fs, tmp_path / "out" / "findings.csv")
    with p.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert list(rows[0]) == list(M.CSV_COLUMNS)
    assert rows[0]["kind"] == "CLASH" and rows[0]["ratio"] == "2.0"
    assert rows[1]["ratio"] == "" and rows[1]["magnitude_mm"] == "3.0"


def test_finding_to_dict_is_json_safe():
    f = _finding()
    f.uid = f.compute_uid()
    d = M.finding_to_dict(f)
    assert json.loads(json.dumps(d))["mech_ref"]["handle"] == "AB"
    assert d["location"] == [1.0, 2.0]
    assert M.finding_to_dict(_finding(st=None))["str_ref"] is None
    assert M.EntityRef.from_dict(d["mech_ref"]) == f.mech_ref


def test_summary_and_message_templates():
    fs = [_finding("CLASH", "CRITICAL"), _finding("CLASH", "INFO"),
          _finding(kind="NOPE", severity="INFO")]
    s = M.summarize(fs)
    assert s["total"] == 3 and s["by_severity"] == {"CRITICAL": 1, "WARNING": 0, "INFO": 2}
    assert s["by_kind"] == {"CLASH": 2, "NOPE": 1}
    assert set(M.KINDS) >= {"CLASH", "MISSING_HOLE", "SYSTEMATIC_OFFSET"}
    assert M.SEVERITIES == ("CRITICAL", "WARNING", "INFO")
    msg = M.format_message("BOLT_HOLE_OFFSET", mech_block="B", mech_index=3,
                           magnitude=6.0, tolerance=1.5, str_block="P")
    assert "6.0 mm" in msg and "1.5 mm" in msg and "{n_bolts}" not in msg


def test_model_finding_defaults_match_the_plan():
    f = M.Finding(kind="CLASH")
    assert (f.severity, f.origin, f.confidence, f.systematic) == \
        ("WARNING", "mismatch", "normal", False)
    assert f.rotation_deg is None and f.ratio is None and f.details == {}


# ═══════════════════════════════════════════════════════════════════════════
# 8b.3 — feature extraction and family assignment (PLAN_MISMATCH §6.2)
#
# Every test below builds its own two-entity DXF rather than leaning on
# tests/synth.py: the point is to pin one behaviour at a time with numbers
# chosen to make a regression obvious.
# ═══════════════════════════════════════════════════════════════════════════

import ezdxf                                                   # noqa: E402
from core.grid import walk_world                                # noqa: E402
from core.mismatch import geom                                  # noqa: E402
from core.mismatch import features as FT                        # noqa: E402
from core.mismatch.checks import Env                            # noqa: E402
from core.mismatch.classify import Classifier, normalize_layer  # noqa: E402
from ezdxf.math import Matrix44                                 # noqa: E402


def _new_doc(insunits: int = 4) -> "ezdxf.document.Drawing":
    """A throwaway drawing.  ``insunits=4`` is millimetres, so one drawing
    unit is one mm and ``mm_per_unit=1`` everywhere below."""
    doc = ezdxf.new("R2013")
    doc.header["$INSUNITS"] = insunits
    return doc


def _extract(doc, *, source="MECH", **kw):
    kw.setdefault("mm_per_unit", 1.0)
    return FT.extract(doc, source=source, **kw)


def _circles(fs):
    return [f for f in fs.features if f.kind == "circle"]


def _by_handle(fs):
    return {f.handle: f for f in fs.features}


def _poly(msp, pts, layer="0", closed=True):
    return msp.add_lwpolyline(pts, format="xyb", close=closed,
                              dxfattribs={"layer": layer})


def _ring_pts(cx, cy, r, n, phase=0.0):
    return [(cx + r * math.cos(phase + 2 * math.pi * i / n),
             cy + r * math.sin(phase + 2 * math.pi * i / n)) for i in range(n)]


# ── circles ───────────────────────────────────────────────────────────────

def test_circle_from_bulge_polyline_and_hexnut():
    """CIRCLE, bulged 2-point LWPOLYLINE, nut polygons and an exploded circle."""
    doc = _new_doc()
    msp = doc.modelspace()

    msp.add_circle((3, 4), 5, dxfattribs={"layer": "BOLT"})
    # two vertices, bulge 1 on both → exactly one full circle
    _poly(msp, [(0, 0, 1.0), (20, 0, 1.0)], "BOLT")            # bulge +1
    _poly(msp, [(0, 60, -1.0), (20, 60, -1.0)], "BOLT")         # bulge −1
    # a nut is a closed polygon of 6 or 8 sides: centre only, no radius
    _poly(msp, _ring_pts(100, 0, 20, 6), "BOLT")
    _poly(msp, _ring_pts(140, 0, 25, 8, math.pi / 8), "BOLT")
    # n >= 12 is an exploded circle: the real radius comes back
    _poly(msp, _ring_pts(200, 0, 30, 16), "BOLT")

    fs = _extract(doc, source="MECH")
    got = _circles(fs)
    by_c = {(round(f.cx, 6), round(f.cy, 6)): f for f in got}
    assert len(got) == 6, sorted(by_c)

    # the plain CIRCLE
    plain = by_c[(3.0, 4.0)]
    assert plain.r == 5.0 and plain.layer == "BOLT"
    assert plain.families == ("mech_bolt",)
    assert plain.size_mm["r"] == 5.0

    # both bulge circles land on the midpoint of the two vertices; the sign
    # of the bulge only picks which of the two semicircles is drawn, and
    # both are the same circle of radius 10 about (10, 0) / (10, 60)
    for (cx, cy), want_r in (((10.0, 0.0), 10.0), ((10.0, 60.0), 10.0)):
        f = by_c[(cx, cy)]
        assert f.r == want_r, (cx, cy, f.r)
        assert f.families == ("mech_bolt",)

    # nuts: no radius at all, but the mean radius still drives the range
    hexf = by_c[(100.0, 0.0)]
    octf = by_c[(140.0, 0.0)]
    assert hexf.r is None and octf.r is None
    assert hexf.size_mm["r"] == pytest.approx(20.0)
    assert octf.size_mm["r"] == pytest.approx(25.0)
    assert hexf.families == ("mech_bolt",) and octf.families == ("mech_bolt",)

    # 16-gon → a real circle again
    ring = by_c[(200.0, 0.0)]
    assert ring.r == pytest.approx(30.0, abs=1e-6)
    assert ring.families == ("mech_bolt",)

    # every circle is addressable through an EntityRef
    for f in got:
        ref = f.entity_ref("mech_bolt")
        assert ref.handle == f.handle and ref.key() == f"{''.join(f.insert_chain)}#{f.handle}"
        assert ref.feature == "circle" and ref.family == "mech_bolt"
        assert ref.layer == "BOLT" and ref.source == "MECH"


def test_extrusion_minus_z_circle_center():
    """A CIRCLE drawn with extrusion (0,0,-1) stores plan coordinates in the
    OCS: ezdxf flips the OCS x axis, so OCS (10,20) is WCS (-10,20)."""
    doc = _new_doc()
    msp = doc.modelspace()
    msp.add_circle((10, 20), 5, dxfattribs={"layer": "BOLT", "extrusion": (0, 0, -1)})
    msp.add_circle((10, 20), 5, dxfattribs={"layer": "BOLT"})          # default +z
    msp.add_arc((0, 40), 7, 0, 360, dxfattribs={"layer": "BOLT",
                                               "extrusion": (0, 0, -1)})

    fs = _extract(doc, source="MECH")
    got = sorted(_circles(fs), key=lambda f: (f.cx, f.cy, f.r))
    assert len(got) == 3, [(f.cx, f.cy, f.r) for f in got]

    # the flipped one is the one with the negative x
    flipped = next(f for f in got if f.cx < 0)
    assert (flipped.cx, flipped.cy, flipped.r) == (-10.0, 20.0, 5.0)

    # the default extrusion is a no-op
    same = next(f for f in got if f.cy == 20.0 and f.cx > 0)
    assert (same.cx, same.cy, same.r) == (10.0, 20.0, 5.0)

    # a near-full arc closes the same way and obeys the same OCS flip
    full_arc = [f for f in got if f.r == 7.0][0]
    assert (full_arc.cx, full_arc.cy) == (0.0, 40.0)

    assert all(f.families == ("mech_bolt",) for f in got)


def test_nonuniform_insert_circle_skipped_and_counted():
    """A non-uniformly scaled INSERT makes every circle in it an ellipse, so
    it is dropped and counted — but a *mirrored* insert (xscale=-1) is not
    non-uniform and must survive."""
    doc = _new_doc()
    blk = doc.blocks.new("BLK-CIRC")
    blk.add_circle((0, 0), 10, dxfattribs={"layer": "BOLT"})
    msp = doc.modelspace()
    msp.add_blockref("BLK-CIRC", (0, 0), dxfattribs={"layer": "BOLT"})
    msp.add_blockref("BLK-CIRC", (100, 0),
                     dxfattribs={"layer": "BOLT", "xscale": 2})      # non-uniform
    msp.add_blockref("BLK-CIRC", (200, 0),
                     dxfattribs={"layer": "BOLT", "xscale": -2, "yscale": 3})
    msp.add_blockref("BLK-CIRC", (300, 0),
                     dxfattribs={"layer": "BOLT", "xscale": -1})     # mirror: fine
    msp.add_blockref("BLK-CIRC", (400, 0),
                     dxfattribs={"layer": "BOLT", "xscale": 3, "yscale": 3})  # uniform

    fs = _extract(doc, source="MECH")
    assert fs.skipped.get("nonuniform", 0) == 2, fs.skipped

    got = sorted(_circles(fs), key=lambda f: f.cx)
    assert [(f.cx, f.cy, f.r) for f in got] == [
        (0.0, 0.0, 10.0),
        (300.0, 0.0, 10.0),      # mirrored, radius unchanged
        (400.0, 0.0, 30.0),      # uniformly scaled
    ]
    assert all(f.families == ("mech_bolt",) for f in got)
    assert all(f.own_layer == "BOLT" for f in got)


# ── rects ─────────────────────────────────────────────────────────────────

def test_rect_detection_mirrored_rotated_insert():
    """A closed 4-point outline becomes a rect through any uniform (even
    mirroring) INSERT; scaling only one axis shears the right angles away
    and the shape falls back to a polygon."""
    doc = _new_doc()
    blk = doc.blocks.new("SKID-1800x1000")
    th = math.radians(20.0)
    base = [(-900.0, -500.0), (900.0, -500.0), (900.0, 500.0), (-900.0, 500.0)]
    local = [(x * math.cos(th) - y * math.sin(th),
              x * math.sin(th) + y * math.cos(th)) for x, y in base]
    _poly(blk, local, "SKID")
    msp = doc.modelspace()
    msp.add_blockref("SKID-1800x1000", (0, 0), dxfattribs={"layer": "SKID"})
    msp.add_blockref("SKID-1800x1000", (10000, 0),
                     dxfattribs={"layer": "SKID", "rotation": 30, "xscale": -1})
    msp.add_blockref("SKID-1800x1000", (20000, 0),
                     dxfattribs={"layer": "SKID", "xscale": 2})

    fs = _extract(doc, source="MECH")
    shapes = sorted(fs.features, key=lambda f: f.cx)
    assert [f.kind for f in shapes] == ["rect", "rect", "polygon"], \
        [(f.kind, f.cx, f.families) for f in fs.features]

    plain = shapes[0]
    assert (plain.cx, plain.cy) == pytest.approx((0.0, 0.0), abs=1e-9)
    assert (plain.w, plain.h) == pytest.approx((1800.0, 1000.0), rel=1e-9)
    assert math.degrees(math.atan2(plain.uy, plain.ux)) % 180.0 == \
        pytest.approx(20.0, abs=1e-9)
    assert plain.families == ("mech_skid",)

    mirrored = shapes[1]
    assert (mirrored.cx, mirrored.cy) == (10000.0, 0.0)
    assert sorted((mirrored.w, mirrored.h)) == pytest.approx([1000.0, 1800.0], rel=1e-9)
    # the axis still follows one of the block's own edges — mirroring reverses
    # the corner winding, so it may come out along the short edge instead
    def _mapped(m, vx, vy):
        return (m[0, 0] * vx + m[1, 0] * vy, m[0, 1] * vx + m[1, 1] * vy)

    im = doc.modelspace()[1].matrix44()
    edges = []
    for i in range(4):
        dx = local[(i + 1) % 4][0] - local[i][0]
        dy = local[(i + 1) % 4][1] - local[i][1]
        ex, ey = _mapped(im, dx, dy)
        edges.append(math.degrees(math.atan2(ey, ex)) % 180.0)
    ang = math.degrees(math.atan2(mirrored.uy, mirrored.ux)) % 180.0
    assert min(abs(ang - e) for e in edges) < 1e-9
    assert mirrored.families == ("mech_skid",)

    # corners must be exactly the local corners pushed through the INSERT
    m = doc.modelspace()[1].matrix44()
    want = sorted((m[0, 0] * x + m[0, 1] * y + m[3, 0],
                   m[1, 0] * x + m[1, 1] * y + m[3, 1]) for x, y in local)
    got = sorted(map(tuple, mirrored.corners()))
    assert np.allclose(got, sorted(want), atol=1e-9)
    assert geom.is_rectangle(mirrored.corners())
    assert abs(geom.polygon_area(mirrored.corners())) == pytest.approx(1.8e6)

    # the sheared one kept its area but lost its right angles
    sheared = shapes[2]
    assert (sheared.cx, sheared.cy) == pytest.approx((20000.0, 0.0), abs=1e-9)
    assert abs(geom.polygon_area(sheared.ring())) == pytest.approx(3.6e6)
    assert not geom.is_rectangle(sheared.ring())
    assert geom.is_convex(sheared.ring())
    assert sheared.families == ("mech_skid",)


def test_walk_world_root_maps_second_drawing_into_mech_world():
    """``root`` is the matrix applied *first* — how the STR drawing is placed
    into MECH world — and it must reach entities nested in blocks."""
    doc = _new_doc()
    inner = doc.blocks.new("INNER")
    inner.add_circle((1, 2), 3)
    outer = doc.blocks.new("OUTER")
    outer.add_blockref("INNER", (10, 20))
    msp = doc.modelspace()
    top = msp.add_blockref("OUTER", (100, 200))
    msp.add_line((0, 0), (7, 0))

    def place(item, x, y):
        """Row-vector mapping of a local point: v' = v · M."""
        return (item.m[0, 0] * x + item.m[1, 0] * y + item.m[3, 0],
                item.m[0, 1] * x + item.m[1, 1] * y + item.m[3, 1])

    items = list(walk_world(doc))
    circ = next(i for i in items if i.entity.dxftype() == "CIRCLE")
    assert (circ.chain, circ.handles) == (
        ("OUTER", "INNER"), (top.dxf.handle, doc.blocks["OUTER"][0].dxf.handle))
    # (1,2) in INNER + (10,20) in OUTER + (100,200) in modelspace
    assert place(circ, 1, 2) == pytest.approx((111.0, 222.0))

    # identity root is the pre-8b.3 behaviour
    assert [(i.chain, place(i, 0, 0)) for i in items] == \
        [((), (0.0, 0.0)), (("OUTER",), (100.0, 200.0)),
         (("OUTER", "INNER"), (110.0, 220.0)), ((), (0.0, 0.0))]

    # a real root shifts every one of them
    root = Matrix44.translate(1000, -500, 0)
    shifted = list(walk_world(doc, root=root))
    assert [(round(place(i, 0, 0)[0]), round(place(i, 0, 0)[1]))
            for i in shifted] == [(1000, -500), (1100, -300), (1110, -280),
                                  (1000, -500)]
    assert place(shifted[2], 1, 2) == pytest.approx((1111.0, -278.0))

    # rotation composes on the left of the INSERT matrix, as v · (I · R)
    c, s = math.cos(math.radians(90.0)), math.sin(math.radians(90.0))
    r = Matrix44((c, s, 0.0, 0.0, -s, c, 0.0, 0.0,
                  0.0, 0.0, 1.0, 0.0, 1000.0, -500.0, 0.0, 1.0))
    circle = [i for i in walk_world(doc, root=r) if i.entity.dxftype() == "CIRCLE"][0]
    assert place(circle, 1, 2) == pytest.approx((778.0, -389.0))

    # extract() takes the same root
    doc2 = _new_doc()
    doc2.modelspace().add_circle((10, 20), 5, dxfattribs={"layer": "BOLT"})
    fs = _extract(doc2, source="MECH", root=Matrix44.translate(7, 9, 0))
    assert _circles(fs)[0].centre() == (17.0, 29.0)


# ── bands ─────────────────────────────────────────────────────────────────

def _frame_doc(ys, layer="FRAME-STEEL", half=2500.0, x0=0.0):
    doc = _new_doc()
    for y in ys:
        doc.modelspace().add_line((x0 - half, y), (x0 + half, y),
                                  dxfattribs={"layer": layer})
    return doc


def test_band_pairing_prefers_flange_edges():
    """Two parallel lines of the right gap make a band; the greedy largest
    gap first is what keeps the web line from stealing a flange."""
    doc = _frame_doc([0.0, 100.0, 400.0])
    fs = _extract(doc, source="STR")
    bands = [f for f in fs.features if f.kind == "band"]

    assert len(bands) == 2, [(f.hw, f.assumed_width) for f in bands]
    widths = sorted(f.hw for f in bands)
    # 100/2 + 400/2 → the flange pair; the lone web line keeps its assumed
    # width.  Pairing the *smallest* gap first would have given 50 instead.
    assert widths == [100.0, 200.0]

    paired = next(f for f in bands if not f.assumed_width)
    assert paired.hw == 200.0
    # the band carries the *centreline* of the pair, y = (0 + 400) / 2
    assert (round(paired.a[1]), round(paired.b[1])) == (200.0, 200.0)
    assert (round(paired.a[0]), round(paired.b[0])) == (-2500.0, 2500.0)
    assert (paired.ux, paired.uy) == (1.0, 0.0)
    assert paired.length() == 5000.0
    assert paired.size_mm["len"] == 5000.0
    assert paired.families == ("str_member",)
    assert paired.entity_ref("str_member").feature == "band"

    lonely = next(f for f in bands if f.assumed_width)
    assert lonely.hw == 100.0
    assert (round(lonely.a[1]), round(lonely.b[1])) == (100.0, 100.0)
    assert lonely.families == ("str_member",)

    # the lines themselves are consumed, not left behind as segments
    assert not [f for f in fs.features if f.kind == "segment"]
    assert fs.counts() == {"str_member": 2}


def test_unpaired_segment_assumed_width():
    """A single long line becomes a band of the assumed flange width; a
    short one does not, and the width comes from the policy."""
    doc = _new_doc()
    msp = doc.modelspace()
    msp.add_line((0, 0), (2000, 0), dxfattribs={"layer": "FRAME-STEEL"})
    msp.add_line((0, 2000), (300, 2000),
                 dxfattribs={"layer": "FRAME-STEEL"})                    # too short
    msp.add_line((0, 900), (2000, 900), dxfattribs={"layer": "DECK-GRID"})

    fs = _extract(doc, source="STR")
    bands = [f for f in fs.features if f.kind == "band"]
    assert len(bands) == 1, [(f.a, f.b, f.layer) for f in bands]

    b = bands[0]
    assert b.assumed_width is True
    assert b.hw == 100.0                                   # default_flange_mm / 2
    assert b.length() == 2000.0 and b.size_mm["len"] == 2000.0
    assert (b.ux, b.uy) == (1.0, 0.0)
    assert b.layer == "FRAME-STEEL" and b.families == ("str_member",)
    assert b.entity_ref("str_member").feature == "band"

    # the assumed width and the length floor both come from the policy
    wider = DEFAULT_POLICY.to_dict()
    wider["band"]["default_flange_mm"] = 300.0
    fs2 = _extract(doc, source="STR", policy=MismatchPolicy.from_dict(wider))
    assert [f.hw for f in fs2.features if f.kind == "band"] == [150.0]

    longer = DEFAULT_POLICY.to_dict()
    longer["band"]["min_member_len_mm"] = 2500.0
    fs3 = _extract(doc, source="STR", policy=MismatchPolicy.from_dict(longer))
    assert not [f for f in fs3.features if f.kind == "band"]


# ── A2/A3/A4 — regressions found in the 2026-10-25 review ──────────────────

def test_member_rect_drawn_short_edge_first_yields_a_full_length_band():
    """A2: a member rectangle's band must span its LONG side.

    ``rect_from_corners`` takes ``u`` from the *first drawn edge*, so a rect
    whose polyline starts on its short side comes back with ``w < h``.  The
    band conversion then projected a segment built along the rect's own axis
    onto the *long* axis, which collapses it to a point: a zero-length band
    whose normal is the zero vector, so ``SUPPORT_OFF_MEMBER`` could never
    fire on it and it won band selection, masking real defects nearby.
    """
    doc = _new_doc()
    msp = doc.modelspace()
    # 5000 x 200, drawn SHORT edge first — the first edge is the 200 mm side.
    _poly(msp, [(-100.0, -2500.0), (100.0, -2500.0),
                (100.0, 2500.0), (-100.0, 2500.0)],
         layer="FRAME-STEEL", closed=True)

    fs = _extract(doc, source="STR")
    bands = [f for f in fs.features if f.kind == "band"]
    assert len(bands) == 1, [(f.kind, f.a, f.b) for f in fs.features]

    b = bands[0]
    assert b.length() == pytest.approx(5000.0), (
        "the band must span the long side of the member rectangle")
    assert b.size_mm["len"] == pytest.approx(5000.0)
    assert b.hw == pytest.approx(100.0)
    # the band's axis is the long axis, and it is a real unit vector
    assert b.ux == pytest.approx(0.0, abs=1e-9)
    assert b.uy == pytest.approx(1.0)
    assert b.families == ("str_member",)


def test_member_rect_drawn_long_edge_first_is_unchanged():
    """The A2 fix must be a no-op for the long-edge-first drawing order."""
    doc = _new_doc()
    msp = doc.modelspace()
    _poly(msp, [(-2500.0, -100.0), (2500.0, -100.0),
                (2500.0, 100.0), (-2500.0, 100.0)],
         layer="FRAME-STEEL", closed=True)

    fs = _extract(doc, source="STR")
    bands = [f for f in fs.features if f.kind == "band"]
    assert len(bands) == 1
    b = bands[0]
    assert b.length() == pytest.approx(5000.0)
    assert b.hw == pytest.approx(100.0)
    assert b.ux == pytest.approx(1.0)
    assert b.uy == pytest.approx(0.0, abs=1e-9)


def test_env_search_radius_cap_is_in_millimetres():
    """A3: the grid-spacing cap must be applied in mm, once.

    ``FitContext.median_spacing_mm`` is already in millimetres; the detector
    used to multiply it by ``mm`` again, so on a metre drawing the cap was
    1000× too loose and the checks searched a thousand times further.
    """
    fs = FT.FeatureSet(source="STR")
    env = Env(policy=DEFAULT_POLICY, mm=1000.0, mech=fs, str=fs,
              clf=Classifier(DEFAULT_POLICY), grid_spacing_mm=6000.0)
    # 0.25 x 6000 mm = 1500 mm = 1.5 drawing units at 1000 mm per unit
    assert env.search_mm(1_000_000.0) == pytest.approx(1500.0)
    assert env.search_u(1_000_000.0) == pytest.approx(1.5)
    # a radius below the cap is untouched
    assert env.search_mm(100.0) == pytest.approx(100.0)
    # with no grid spacing there is no cap at all
    env2 = Env(policy=DEFAULT_POLICY, mm=1000.0, mech=fs, str=fs,
               clf=Classifier(DEFAULT_POLICY))
    assert env2.search_mm(1_000_000.0) == pytest.approx(1_000_000.0)


def test_detect_does_not_convert_the_grid_spacing_twice(monkeypatch):
    """A3: the value `detect()` hands to `Env` must already be in mm.

    This is the call-site half of A3: `FitContext.median_spacing_mm` is
    already millimetres, and `detect()` used to multiply it by `mm` again.
    The difference only shows on a metre drawing, so the MECH document here
    is in metres.
    """
    import core.mismatch as MM
    from core.mismatch import checks as checks_mod
    from core.transform2d import Affine2D

    seen = {}
    real_env = checks_mod.Env

    class _SpyEnv(real_env):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen["grid_spacing_mm"] = kwargs.get("grid_spacing_mm")
            seen["mm"] = kwargs.get("mm")

    monkeypatch.setattr(checks_mod, "Env", _SpyEnv)

    # a MECH drawing in metres ($INSUNITS = 6)
    mech = _new_doc(insunits=6)
    mech.modelspace().add_line((0, 0), (1000, 0), dxfattribs={"layer": "X"})
    str_doc = _new_doc(insunits=6)
    str_doc.modelspace().add_line((0, 0), (1000, 0), dxfattribs={"layer": "X"})

    # transform.json's `median_spacing` is in MECH drawing units: 6000 m.
    fit_ctx = {"median_spacing": 6000.0, "rms_residual": 0.0,
               "correspondences": [], "quality": {"status": "ok"}}

    MM.detect(mech, str_doc, Affine2D(), fit_ctx=fit_ctx)

    assert seen["mm"] == 1000.0, "the MECH document must be read as metres"
    # 6000 m of grid spacing == 6 000 000 mm.  The cap is that number, NOT
    # 6 000 000 × 1000.
    assert seen["grid_spacing_mm"] == pytest.approx(6_000_000.0)


def test_clash_with_members_detects_a_band_overlap():
    """A4: ``clash_with_members=True`` must actually test the members.

    ``str_member`` features are *bands*, and ``polygon_of()`` returned an
    empty array for anything that was not a rect or a polygon — so the bands
    were silently skipped and the flag never found a clash.
    """
    from core.mismatch.checks import clash as clash_mod

    # a STR beam drawn as two parallel lines 200 mm apart
    str_doc = _new_doc()
    for y in (100.0, 300.0):
        str_doc.modelspace().add_line((-2500.0, y), (2500.0, y),
                                      dxfattribs={"layer": "FRAME-STEEL"})
    # a MECH equipment rectangle sitting on top of the beam
    mech_doc = _new_doc()
    _poly(mech_doc.modelspace(), [(-500.0, 150.0), (500.0, 150.0),
                                  (500.0, 250.0), (-500.0, 250.0)],
         layer="EQUIPMENT", closed=True)

    pol = DEFAULT_POLICY.to_dict()
    pol["clash_with_members"] = True
    policy = MismatchPolicy.from_dict(pol)

    mech_fs = _extract(mech_doc, source="MECH", policy=policy)
    str_fs = _extract(str_doc, source="STR", policy=policy)
    env = Env(policy=policy, mm=1.0, mech=mech_fs, str=str_fs,
              clf=Classifier(policy))

    bands = [f for f in str_fs.features if f.kind == "band"]
    assert bands, "the STR beam must produce a band"

    findings = clash_mod.run(env)
    clashes = [f for f in findings if f.kind == "CLASH"]
    assert clashes, (
        "a band overlapping the equipment must be reported as a CLASH when "
        "clash_with_members is enabled")
    assert clashes[0].severity == "CRITICAL"
    assert clashes[0].magnitude_mm is not None and clashes[0].magnitude_mm > 0.0


# ── geometry helpers ──────────────────────────────────────────────────────

def _square(cx, cy, side, deg=0.0):
    return geom.rect_corners((cx, cy), (math.cos(math.radians(deg)),
                                        math.sin(math.radians(deg))), side, side)


def _turn(pts, deg):
    """Rotate points about the origin (row-vector convention, CCW)."""
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.asarray(pts, dtype=float) @ np.array([[c, s], [-s, c]])


def test_sat_penetration_axis_aligned_and_rotated():
    """SAT returns the shallowest overlap and the vector that resolves it by
    moving polygon *a* out of *b*; rotating both together must not change
    the depth."""
    big, small = _square(-570.0, 0.0, 1000.0), _square(0.0, 0.0, 300.0)

    hit = geom.sat_penetration(big, small)
    assert hit is not None
    depth, mtv = hit
    assert depth == pytest.approx(80.0)
    assert tuple(mtv) == pytest.approx((-80.0, 0.0))

    # resolving the penetration really separates them
    assert geom.sat_penetration(big + mtv, small) is None
    # and it is symmetric: b may be the mover, same depth, opposite vector
    d2, m2 = geom.sat_penetration(small, big)
    assert d2 == pytest.approx(depth)
    assert np.allclose(m2, -mtv)

    # rotate the *whole configuration* 30° — same separation, same depth,
    # same vector turned with it
    big30, small30 = _turn(big, 30.0), _turn(small, 30.0)
    d30, m30 = geom.sat_penetration(big30, small30)
    assert d30 == pytest.approx(80.0)
    assert np.allclose(m30, _turn(mtv, 30.0))
    assert np.allclose(geom.unit(m30), (-math.cos(math.radians(30)),
                                        -math.sin(math.radians(30))))
    assert geom.sat_penetration(big30 + m30, small30) is None

    # a rotated square against an axis-aligned one: same guarantees, but the
    # depth is no longer a round number — check the invariants only
    d4, m4 = geom.sat_penetration(big30, small)
    assert d4 > 0.0 and np.linalg.norm(m4) == pytest.approx(d4)
    assert geom.sat_penetration(big30 + m4, small) is None

    # separated, and touching exactly, both report no penetration
    assert geom.sat_penetration(_square(-1000.0, 0.0, 1000.0), small) is None
    assert geom.sat_penetration(_square(-300.0, 0.0, 300.0), small) is None
    assert geom.sat_penetration(_square(-299.5, 0.0, 300.0), small)[0] == \
        pytest.approx(0.5)


def test_vertex_outside_distance():
    """Signed distance: positive outside the polygon, negative inside, zero
    on the boundary."""
    outline = np.array([(0.0, 0.0), (1000.0, 0.0), (1000.0, 1000.0), (0.0, 1000.0)])

    verts = np.array([(500.0, 500.0),      # dead centre      → −500
                      (0.0, 500.0),        # on the edge      →    0
                      (1100.0, 500.0),     # 100 to the right →  100
                      (1000.0, -50.0),     # 50 below         →   50
                      (-30.0, 1000.0),     # 30 left          →   30
                      (1000.0, 2000.0)])   # a corner, off the side →  1000
    d = geom.signed_boundary_distances(verts, outline)
    assert d == pytest.approx([-500.0, 0.0, 100.0, 50.0, 30.0, 1000.0])

    # membership agrees with the sign
    assert [geom.point_in_polygon(v, outline) for v in verts] == \
        [True, True, False, False, False, False]

    # a real skid offset: an 1800×1000 skid reaches x = 900, so a vertex
    # 250 beyond it is 250 outside
    skid = geom.rect_corners((0.0, 0.0), (1.0, 0.0), 1800.0, 1000.0)
    assert geom.signed_boundary_distances(np.array([(1150.0, 0.0)]), skid) \
        == pytest.approx([250.0])

    # containment is a *vertex* fraction: it has to work for a non-convex
    # counterpart outline, where a convex clip would be wrong
    plinth = geom.rect_corners((0.0, 0.0), (1.0, 0.0), 2000.0, 1200.0)
    for (cx, cy, w, h, frac) in [(0.0, 0.0, 1800.0, 1000.0, 1.0),
                                 (900.0, 0.0, 400.0, 400.0, 0.5),
                                 (5000.0, 0.0, 100.0, 100.0, 0.0)]:
        box = geom.rect_corners((cx, cy), (1.0, 0.0), w, h)
        assert geom.polygon_overlap_fraction(box, plinth) == pytest.approx(frac)

    # a convex counterpart uses the area instead: the 1800×1000 skid on the
    # 2000×1200 plinth is wholly inside, and a 400×400 box straddling the
    # plinth edge keeps 300×400 of its 160000 mm²
    skid_box = geom.rect_corners((0.0, 0.0), (1.0, 0.0), 1800.0, 1000.0)
    straddle = geom.rect_corners((900.0, 0.0), (1.0, 0.0), 400.0, 400.0)
    assert geom.convex_clip_area(skid_box, plinth) == pytest.approx(1.8e6)
    assert geom.convex_clip_area(straddle, plinth) == pytest.approx(1.2e5)
    assert geom.convex_clip_area(
        geom.rect_corners((9000.0, 0.0), (1.0, 0.0), 100.0, 100.0), plinth) == 0.0

    # …and rotating a shape that still fits does not change the verdict
    rot = geom.rect_corners((0.0, 0.0),
                            (math.cos(math.radians(10)), math.sin(math.radians(10))),
                            1500.0, 900.0)
    assert geom.polygon_overlap_fraction(rot, plinth) == pytest.approx(1.0)
    assert geom.convex_clip_area(rot, plinth) == pytest.approx(1.35e6)


# ── layer-0 inheritance ───────────────────────────────────────────────────

def test_layer_zero_inherits_insert_layer():
    """An entity on layer "0" inherits the nearest enclosing INSERT's
    non-"0" layer; display prefixes are stripped on the way in."""
    doc = _new_doc()
    hole = doc.blocks.new("XB-1")                 # name matches no block regex
    hole.add_circle((0, 0), 11, dxfattribs={"layer": "0"})

    plate = doc.blocks.new("PLATE-1")
    plate.add_blockref("XB-1", (50, 0), dxfattribs={"layer": "STEEL-HOLE"})

    msp = doc.modelspace()
    msp.add_blockref("XB-1", (0, 0), dxfattribs={"layer": "STEEL-HOLE"})
    msp.add_blockref("XB-1", (100, 0), dxfattribs={"layer": "STR|STEEL-HOLE"})
    msp.add_blockref("XB-1", (200, 0), dxfattribs={"layer": "0"})     # inherits nothing
    msp.add_blockref("PLATE-1", (300, 0), dxfattribs={"layer": "STEEL-PLATE"})

    fs = _extract(doc, source="STR")
    got = sorted(_circles(fs), key=lambda f: f.cx)

    assert [(f.cx, f.cy, f.layer, f.own_layer, f.families) for f in got] == [
        (0.0, 0.0, "STEEL-HOLE", None, ("str_hole",)),
        (100.0, 0.0, "STEEL-HOLE", None, ("str_hole",)),     # prefix stripped
        (350.0, 0.0, "STEEL-HOLE", None, ("str_hole",)),     # nested in PLATE-1
    ]
    # the bare layer-"0" insert is claimed by nothing at all
    assert [f for f in fs.features if f.cx == 200.0] == []
    assert fs.skipped.get("prefilter", 0) >= 1

    # every ref reports the *effective* layer, so 8b.4 can show it in the UI
    assert got[0].entity_ref("str_hole").layer == "STEEL-HOLE"

    # normalize_layer is idempotent and only strips from the front
    assert normalize_layer(normalize_layer("STR$0$STR|STEEL-HOLE")) == "STEEL-HOLE"
    assert normalize_layer("MECH|BOLT") == "BOLT"
    assert normalize_layer("STR|STEEL-HOLE") == "STEEL-HOLE"
    assert normalize_layer("STEEL-HOLE") == "STEEL-HOLE"
    assert normalize_layer("") == "" and normalize_layer(None) == ""

    # the prefilter rejects by (layer, block chain) alone
    clf = Classifier()
    assert clf.candidates("STEEL-HOLE", ("XB-1",)) >= {"str_hole", "mech_plate_hole"}
    assert clf.candidates("STEEL-HOLE", ("XB-1",)) == \
        clf.candidates("STR|STEEL-HOLE", ("XB-1",))
    assert clf.candidates("NONSENSE-LAYER", ()) == frozenset()
    # two distinct keys: the prefixed and bare names share one cache slot
    assert clf.prefilter_cache_size() == 2