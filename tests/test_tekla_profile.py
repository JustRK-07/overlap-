#!/usr/bin/env python3
"""tests/test_tekla_profile.py — a Tekla-style STR drawing.

A CAD export puts everything on layer 0 and names its blocks
``<prefix><id> - <id>``.  The prefix is the only stable part of that name,
so the rules match on the prefix, and a part's geometry — a set of LINE
primitives, never a closed polyline — is read as the oriented bounding box
of those primitives.

Three things have to hold:

* a long thin part is a **member**, a small one a **plate**, and a hole is
  a hole;
* an annotation, grid or reference block is **never geometry**;
* the inventory accounts for every INSERT.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import ezdxf
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from core.mismatch import build_inventory                      # noqa: E402
from core.mismatch.classify import Classifier                 # noqa: E402
from core.mismatch.features import extract_pair                # noqa: E402
from core.mismatch.policy import DEFAULT_POLICY, apply_profile  # noqa: E402
from core.transform2d import Affine2D                         # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# a Tekla-style drawing
# ─────────────────────────────────────────────────────────────────────────────

def _new() -> "ezdxf.document.Drawing":
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4          # millimetres
    return doc


def _line_rect(doc, name, w, h):
    """A block drawn as four LINEs — how a CAD export draws a part."""
    blk = doc.blocks.new(name)
    blk.add_line((0.0, 0.0), (w, 0.0))
    blk.add_line((w, 0.0), (w, h))
    blk.add_line((w, h), (0.0, h))
    blk.add_line((0.0, h), (0.0, 0.0))


def _build(tmp_path) -> Path:
    """A Tekla-style STR drawing, saved."""
    doc = _new()

    # ── the parts ────────────────────────────────────────────────────
    _line_rect(doc, "Part-116860372-114953 - 114867", 2000.0, 150.0)  # a beam
    _line_rect(doc, "Part-116860372-114954 - 114867", 200.0, 150.0)   # a plate
    hole = doc.blocks.new("Part-116860372-114955 - 114867")
    hole.add_circle((0.0, 0.0), 100.0)                                # a tube

    # ── a bolt, a connection, the annotation ──────────────────────────
    bolt = doc.blocks.new("Bolt-116860372-115384 - 114867")
    bolt.add_circle((0.0, 0.0), 13.0)
    doc.blocks.new("Connection-116860372-115933 - 114867")      # an empty block

    mark = doc.blocks.new("Mark-116860372-116000 - 114867")
    mark.add_line((0.0, 0.0), (100.0, 0.0))
    mark.add_text("NOTE 1", dxfattribs={"height": 2.5})

    dim = doc.blocks.new("StraightDimension-116860372-116100 - 114867")
    dim.add_line((0.0, 0.0), (500.0, 0.0))

    grid = doc.blocks.new("GridLine-116860372-116200 - 114867")
    grid.add_line((0.0, 0.0), (6000.0, 0.0))
    grid.add_arc((0.0, 0.0), 100.0, 0.0, 180.0)

    msp = doc.modelspace()
    # every INSERT sits on layer 0, like a Tekla export
    for name, x, y in (
        ("Part-116860372-114953 - 114867", 0.0, 0.0),
        ("Part-116860372-114954 - 114867", 0.0, 1000.0),
        ("Part-116860372-114955 - 114867", 0.0, 2000.0),
        ("Bolt-116860372-115384 - 114867", 5000.0, 0.0),
        ("Connection-116860372-115933 - 114867", 5000.0, 1000.0),
        ("GridLine-116860372-116200 - 114867", 5000.0, 2000.0),
        ("StraightDimension-116860372-116100 - 114867", 9000.0, 0.0),
    ):
        msp.add_blockref(name, (x, y), dxfattribs={"layer": "0"})
    # enough marks for the profile's signature (a Tekla export has hundreds)
    for i in range(12):
        msp.add_blockref("Mark-116860372-116000 - 114867", (0.0, 3000.0 + i * 10.0),
                         dxfattribs={"layer": "0"})

    path = tmp_path / "tekla_str.dxf"
    doc.saveas(path)
    return path


def _extract(path):
    """The STR features of the drawing, with the profile applied."""
    doc = ezdxf.readfile(str(path))
    pol = apply_profile(DEFAULT_POLICY, doc)
    assert pol.profile == "tp104", "the profile did not match the drawing"
    clf = Classifier(pol)
    mech = _new()
    _mfs, sfs = extract_pair(mech, doc, affine=Affine2D(), policy=pol,
                             classifier=clf, mm_per_unit=1.0)
    return doc, clf, sfs


# ─────────────────────────────────────────────────────────────────────────────
# the classification
# ─────────────────────────────────────────────────────────────────────────────

def test_a_tekla_str_is_classified(tmp_path):
    """A long thin part is a member, a small one a plate, a hole a hole."""
    _doc, _clf, sfs = _extract(_build(tmp_path))
    counts = sfs.counts()

    assert counts.get("str_member") == 1, counts
    assert counts.get("str_plate") == 1, counts
    assert counts.get("str_circle_section") == 1, counts
    assert counts.get("str_bolt") == 1, counts


def test_a_member_is_a_band_and_a_plate_is_a_rect(tmp_path):
    """The shape follows the box: long and thin is a band, small a rect."""
    _doc, _clf, sfs = _extract(_build(tmp_path))
    by_family = {}
    for f in sfs.features:
        for fam in f.families:
            by_family.setdefault(fam, []).append(f)

    member = by_family["str_member"][0]
    assert member.kind == "band"
    assert member.size_mm["len"] == pytest.approx(2000.0)
    assert member.hw * 2 == pytest.approx(150.0)

    plate = by_family["str_plate"][0]
    assert plate.kind == "rect"
    assert plate.size_mm["w"] == pytest.approx(200.0)
    assert plate.size_mm["h"] == pytest.approx(150.0)

    tube = by_family["str_circle_section"][0]
    assert tube.kind == "circle"
    assert tube.r == pytest.approx(100.0)

    bolt = by_family["str_bolt"][0]
    assert bolt.kind == "circle"
    assert bolt.r == pytest.approx(13.0)


def test_the_annotation_prefixes_are_never_geometry(tmp_path):
    """A mark, a dimension and a grid line are not geometry."""
    doc, clf, sfs = _extract(_build(tmp_path))

    blocks = {f.block for f in sfs.features}
    assert not any(b.startswith("Mark-") for b in blocks), blocks
    assert not any(b.startswith("StraightDimension-") for b in blocks), blocks
    assert not any(b.startswith("GridLine-") for b in blocks), blocks
    # and the rules say so
    for prefix in ("Mark-", "StraightDimension-", "SectionMark-",
                   "GridLine-", "ReferenceModel-", "Text-"):
        rule = clf.prefix_rule([prefix + "x - 1"])
        assert rule is not None, prefix
        assert rule["kind"] in ("annotation", "grid", "reference"), prefix


def test_a_part_is_not_assumed_to_be_a_beam(tmp_path):
    """Not every Part- is a member — the shape decides."""
    doc, clf, sfs = _extract(_build(tmp_path))
    counts = sfs.counts()
    # three parts, three different families
    assert counts.get("str_member", 0) == 1
    assert counts.get("str_plate", 0) == 1
    assert counts.get("str_circle_section", 0) == 1


# ─────────────────────────────────────────────────────────────────────────────
# the inventory
# ─────────────────────────────────────────────────────────────────────────────

def test_the_inventory_accounts_for_every_insert(tmp_path):
    """The per-prefix counts sum to the number of INSERTs."""
    path = _build(tmp_path)
    doc, clf, sfs = _extract(path)
    inv = build_inventory(doc, sfs, clf, "STR")

    inserts = sum(1 for e in doc.modelspace() if e.dxftype() == "INSERT")
    assert inv["inserts"] == inserts
    assert sum(inv["prefixes"].values()) == inserts
    assert inv["prefixes"]["Part-"] == 3
    assert inv["prefixes"]["Bolt-"] == 1
    assert inv["prefixes"]["Connection-"] == 1
    assert inv["prefixes"]["Mark-"] == 12
    assert inv["prefixes"]["GridLine-"] == 1
    assert inv["prefixes"]["StraightDimension-"] == 1


def test_the_inventory_reports_what_was_not_recognised(tmp_path):
    """The annotation prefixes are inventoried, with the reason."""
    doc, clf, sfs = _extract(_build(tmp_path))
    inv = build_inventory(doc, sfs, clf, "STR")

    unrecognised = {u["prefix"]: u for u in inv["unrecognised"]}
    assert "Mark-" in unrecognised
    assert unrecognised["Mark-"]["inserts"] == 12
    assert "annotation" in unrecognised["Mark-"]["why"]
    assert "GridLine-" in unrecognised
    assert "never geometry" in unrecognised["GridLine-"]["why"]
    # the parts are recognised
    assert "Part-" not in unrecognised


def test_the_inventory_counts_the_families(tmp_path):
    """Per family: the features, and the blocks that belong to them."""
    doc, clf, sfs = _extract(_build(tmp_path))
    inv = build_inventory(doc, sfs, clf, "STR")

    assert inv["families"]["str_member"] == 1
    assert inv["families"]["str_plate"] == 1
    assert inv["blocks_by_family"]["str_bolt"] == 1
    # the connection block is inventoried even though it is empty
    assert inv["blocks_by_family"]["str_connection"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# the profile itself
# ─────────────────────────────────────────────────────────────────────────────

def test_the_profile_is_a_valid_policy(tmp_path):
    """The profile parses, and the policy it produces is well formed."""
    from core.mismatch.policy import Family, load_profile

    spec = load_profile("tp104")
    assert spec is not None
    assert "Part-" in spec["prefixes"]
    assert spec["prefixes"]["Part-"]["shape"] == "obb"
    assert spec["prefixes"]["Mark-"]["kind"] == "annotation"
    # every family the profile names exists and is well formed
    for name, fam in spec["families"].items():
        parsed = Family.from_dict(name, fam, "profile")
        assert parsed.src == "STR"
        assert parsed.block_prefixes


def test_a_drawing_without_the_signature_keeps_the_default_rules(tmp_path):
    """A drawing that is not a Tekla export is left alone."""
    doc = _new()
    blk = doc.blocks.new("Part-1")
    blk.add_line((0.0, 0.0), (10.0, 0.0))
    doc.modelspace().add_blockref("Part-1", (0.0, 0.0), dxfattribs={"layer": "0"})
    path = tmp_path / "not_tekla.dxf"
    doc.saveas(path)

    doc = ezdxf.readfile(str(path))
    pol = apply_profile(DEFAULT_POLICY, doc)
    assert pol.profile is None or pol.profile == "tp104"
    assert pol.prefixes == {}, "the profile must not touch a foreign drawing"
