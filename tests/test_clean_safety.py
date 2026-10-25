#!/usr/bin/env python3
"""test_clean_safety.py — step 3: cleaning never deletes structure by shape/prefix alone."""
from __future__ import annotations

import sys
from pathlib import Path

import ezdxf
import pytest
from ezdxf import disassemble

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import synth  # noqa: E402
from core import clean_v1, clean_v2  # noqa: E402
from core.clean_common import layer_matches, reachable_blocks  # noqa: E402

PREFIX = "TP104-"


@pytest.fixture(params=[clean_v1, clean_v2], ids=["v1", "v2"])
def cleaner(request):
    return request.param


def _clean(cleaner, src, tmp_path, *, is_str):
    out = tmp_path / f"{src.stem}_{cleaner.__name__.split('.')[-1]}_clean.dxf"
    stats = cleaner._clean_one(src, out, is_str=is_str)
    return ezdxf.readfile(str(out)), stats


def _decomp(doc, layer=None):
    return [e for e in disassemble.recursive_decompose(doc.modelspace())
            if layer is None or e.dxf.layer == layer]


def test_layer_matches_semantics():
    assert layer_matches("REV-A", "REV-")
    assert layer_matches("am_5", "AM_")
    assert layer_matches("AM_VIEWS", "am_*")
    assert layer_matches("DIM", "DIM")
    assert not layer_matches("DIMS", "DIM")          # exact, not prefix
    assert not layer_matches("FRAME-STEEL", "FRAME")
    assert not layer_matches("CIVIL-FOUNDATION", "CIVIL")
    assert layer_matches("FRAME", "frame")


def test_default_annotation_layers_have_no_bare_structural_words():
    from core.clean_policy import DEFAULT_POLICY
    ups = [x.upper() for x in DEFAULT_POLICY.annotation_layers]
    assert "FRAME" not in ups and "CIVIL" not in ups


def test_gusset_triangle_and_solid_survive(cleaner, fx_synth_hard, tmp_path):
    out, _ = _clean(cleaner, fx_synth_hard.str_path, tmp_path, is_str=True)
    tri = [e for e in _decomp(out, synth.LAYER_PLATE)
           if e.dxftype() == "LWPOLYLINE" and len(e) == 3]
    solid = [e for e in _decomp(out, synth.LAYER_FILL) if e.dxftype() == "SOLID"]
    assert len(tri) == 1 and len(solid) == 1


def test_revision_triangle_removed(cleaner, fx_synth_hard, tmp_path):
    src = ezdxf.readfile(str(fx_synth_hard.mech_path))
    assert len(_decomp(src, synth.LAYER_REV)) == 1
    out, _ = _clean(cleaner, fx_synth_hard.mech_path, tmp_path, is_str=False)
    assert _decomp(out, synth.LAYER_REV) == []


def test_frame_and_civil_layers_survive(cleaner, fx_synth_hard, tmp_path):
    src = ezdxf.readfile(str(fx_synth_hard.str_path))
    out, _ = _clean(cleaner, fx_synth_hard.str_path, tmp_path, is_str=True)
    for layer in (synth.LAYER_BEAM, synth.LAYER_FOOTING):
        n = len(_decomp(src, layer))
        assert n > 0
        assert len(_decomp(out, layer)) == n, layer


def test_annotation_prefix_still_matches_am_and_rev(cleaner, tmp_path):
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    for lay in ("AM_5", "REV-B", "DIM", "TITLE", "KEEP"):
        doc.layers.add(lay)
        msp.add_line((0, 0), (1, 1), dxfattribs={"layer": lay})
    src = tmp_path / "ann.dxf"
    doc.saveas(src)
    out, _ = _clean(cleaner, src, tmp_path, is_str=False)
    layers = {e.dxf.layer for e in out.modelspace()}
    if cleaner is clean_v1:       # v2 strips only annotation-layer TEXT, not geometry
        assert layers == {"KEEP"}
    else:
        assert "KEEP" in layers


def test_shape_alone_not_removed_but_marker_context_is(cleaner, tmp_path):
    doc = ezdxf.new("R2018")
    doc.layers.add("PLATE")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (1, 0), (0, 1)], close=True, dxfattribs={"layer": "PLATE"})
    mk = doc.blocks.new("Mark-1")
    mk.add_solid([(0, 0), (1, 0), (0, 1)], dxfattribs={"layer": "PLATE"})
    msp.add_blockref("Mark-1", (5, 5))
    src = tmp_path / "tri.dxf"
    doc.saveas(src)
    out, _ = _clean(cleaner, src, tmp_path, is_str=False)
    types = [e.dxftype() for e in out.modelspace()]
    assert types.count("LWPOLYLINE") == 1
    # MECH keeps only A$C/RTR inserts, so Mark-1 INSERT is gone; check block-level rule directly.
    from core.clean_common import _strip_triangles_in
    blk = ezdxf.readfile(str(src)).blocks.get("Mark-1")
    assert _strip_triangles_in(blk, container_name="Mark-1") == 1


def test_mtext_grid_label_survives(cleaner, fx_synth_hard, tmp_path):
    out, _ = _clean(cleaner, fx_synth_hard.mech_path, tmp_path, is_str=False)
    mtexts = [e.plain_text().strip() for e in out.modelspace().query("MTEXT")]
    assert f"{PREFIX}{fx_synth_hard.opts.mtext_label}" in mtexts


def test_attrib_grid_label_survives_and_non_grid_attrib_removed(cleaner, fx_synth_hard, tmp_path):
    # Add a non-grid ATTRIB to the GridLine INSERT carrying the grid ATTRIBs.
    doc = ezdxf.readfile(str(fx_synth_hard.str_path))
    ins = next(e for e in doc.modelspace().query("INSERT") if e.attribs)
    ins.add_attrib("NOTE", "BEAM-NOTE", (0, 0))
    src = tmp_path / "attr_in.dxf"
    doc.saveas(src)
    out, _ = _clean(cleaner, src, tmp_path, is_str=True)
    texts = [a.dxf.text.strip() for i in out.modelspace().query("INSERT") for a in i.attribs]
    assert f"{PREFIX}{fx_synth_hard.opts.attrib_label}" in texts
    assert "BEAM-NOTE" not in texts


def test_beam_text_still_removed(cleaner, fx_synth_hard, tmp_path):
    out, _ = _clean(cleaner, fx_synth_hard.str_path, tmp_path, is_str=True)
    assert all(e.dxf.text.strip() != "ISMB 600" for e in out.modelspace().query("TEXT"))


def test_block_referenced_only_from_anonymous_block_kept(cleaner, fx_synth_hard, tmp_path):
    out, _ = _clean(cleaner, fx_synth_hard.str_path, tmp_path, is_str=True)
    assert "Part-ANCHOR" in out.blocks


def test_unreachable_block_removed(cleaner, fx_synth_hard, tmp_path):
    src = ezdxf.readfile(str(fx_synth_hard.str_path))
    assert "Part-ORPHAN" in src.blocks
    out, _ = _clean(cleaner, fx_synth_hard.str_path, tmp_path, is_str=True)
    assert "Part-ORPHAN" not in out.blocks


def test_reachable_blocks_walks_anonymous_and_paperspace():
    doc = ezdxf.new("R2018")
    for n in ("A", "B", "C", "D"):
        doc.blocks.new(n).add_line((0, 0), (1, 1))
    doc.blocks.get("A").add_blockref("B", (0, 0))
    anon = doc.blocks.new("*U7")
    anon.add_blockref("C", (0, 0))
    doc.modelspace().add_blockref("A", (0, 0))
    doc.modelspace().add_blockref("*U7", (0, 0))
    r = reachable_blocks(doc)
    assert {"A", "B", "C", "*U7"} <= r and "D" not in r
    doc.layout("Layout1").add_blockref("D", (0, 0))
    assert "D" in reachable_blocks(doc)


def test_insunits_recorded(cleaner, fx_synth_hard, tmp_path):
    _, mstats = _clean(cleaner, fx_synth_hard.mech_path, tmp_path, is_str=False)
    _, sstats = _clean(cleaner, fx_synth_hard.str_path, tmp_path, is_str=True)
    assert mstats["insunits"] == 4
    assert sstats["insunits"] == 6
