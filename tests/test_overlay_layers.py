#!/usr/bin/env python3
"""test_overlay_layers.py — core/overlay.py step 7.

Per-source layers (``MECH|<layer>`` / ``STR|<layer>``), the two colour
modes, the STR lineweight/linetype options and the ``QA|GRID`` overlay.

Most tests drive :func:`core.overlay.overlay_docs` +
:func:`core.overlay.apply_source_layers` directly on the generator output of
``tests/synth.py``; the two that need a fit (``grid_mech.json`` /
``grid_str.json``) run the real stages on a job.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import ezdxf
import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import synth                                  # noqa: E402
from core import dxfio                         # noqa: E402
from core import fit as fit_mod                # noqa: E402
from core import grid as grid_mod              # noqa: E402
from core import overlay as overlay_mod        # noqa: E402
from core.overlay import (                     # noqa: E402
    OverlayStyle, QA_GRID_LAYER, QA_GRID_STR_LAYER, QA_OUTLIER_LAYER,
    SOURCE_LAYER_SEP, apply_source_layers, draw_qa_grid, overlay_docs,
)
from core.transform2d import Affine2D          # noqa: E402

PREFIX = "TP104-"
AXES = ["A", "B", "C", "D", "1", "2", "3"]
KEEP = ("0", "Defpoints")


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _truth(pair) -> Affine2D:
    return Affine2D(*pair.truth.as_affine())


def _overlay(pair, *, style=None, prep=None):
    """(merged_doc, str_doc, report) after overlaying the generator files.

    ``prep(mech_doc, str_doc)`` may set source layer properties before the
    overlay, which is how the "properties are copied" tests colour the
    layers of the two drawings differently.
    """
    mech = ezdxf.readfile(str(pair.mech_path))
    str_doc = ezdxf.readfile(str(pair.str_path))
    if prep is not None:
        prep(mech, str_doc)
    report = overlay_docs(mech, str_doc, _truth(pair), style=style)
    return mech, str_doc, report


def _layer(doc, name):
    return doc.layers.get(name) if doc.layers.has_entry(name) else None


def _names(doc):
    return {l.dxf.name for l in doc.layers}


def _set_props(doc, layer_name, *, color=None, linetype=None, lineweight=None,
               plot=None, off=False, frozen=False, locked=False):
    layer = doc.layers.new(layer_name) if not doc.layers.has_entry(layer_name) \
        else doc.layers.get(layer_name)
    if color is not None:
        layer.dxf.color = color
    if linetype is not None:
        _add_linetype(doc, linetype)
        layer.dxf.linetype = linetype
    if lineweight is not None:
        layer.dxf.lineweight = lineweight
    if plot is not None:
        layer.dxf.plot = plot
    if off:
        layer.off()
    if frozen:
        layer.freeze()
    if locked:
        layer.lock()
    return layer


def _add_linetype(doc, name, pattern=(0.5, -0.25)):
    if name not in doc.linetypes:
        doc.linetypes.add(name, pattern=list(pattern),
                          description=f"{name} (test)")
    return name


def _snapshot(layer) -> dict:
    """The properties apply_source_layers has to carry over to the copy."""
    return {
        "color": layer.dxf.color,
        "linetype": layer.dxf.linetype,
        "lineweight": layer.dxf.lineweight,
        "plot": layer.dxf.plot,
        "off": layer.is_off(),
        "frozen": layer.is_frozen(),
        "locked": layer.is_locked(),
    }


def _blocks_named(doc, prefix="STR"):
    return {b.name: b for b in doc.blocks
            if not b.name.startswith(("*Model_Space", "*Paper_Space"))}


def _centers(msp, layer, dxftype):
    """[(x, y)] of the centre of every entity of `dxftype` on `layer`."""
    out = []
    for e in msp:
        if e.dxf.layer == layer and e.dxftype() == dxftype:
            out.append((e.dxf.center.x, e.dxf.center.y))
    return out


def _x_centers(msp, layer, size=None):
    """[(x, y)] — one per X marker (two crossing short LINEs) on `layer`.

    ``size`` (the marker half-diagonal) skips the long gridline LINEs that
    share the layer.
    """
    lines = [e for e in msp if e.dxf.layer == layer and e.dxftype() == "LINE"]
    if size is not None:
        limit = 3.0 * size
        lines = [e for e in lines
                 if np.hypot(e.dxf.end.x - e.dxf.start.x,
                             e.dxf.end.y - e.dxf.start.y) <= limit]
    assert len(lines) % 2 == 0, "an X marker lost one of its two lines"
    out = []
    for line in lines:
        mx = (line.dxf.start.x + line.dxf.end.x) / 2.0
        my = (line.dxf.start.y + line.dxf.end.y) / 2.0
        if not any(abs(mx - x) < 1e-6 and abs(my - y) < 1e-6 for x, y in out):
            out.append((mx, my))
    return out


def _worst(got, want, tol=1e-6):
    """Largest distance between each `want` point and the nearest `got` one."""
    a = np.asarray(got, dtype=float)
    b = np.asarray(want, dtype=float)
    if not len(a) or not len(b):
        return float("inf")
    return float(np.hypot(*(a[:, None, :] - b[None, :, :]).T).min(axis=0).max())


def _run_job(fx_synth_job, opts=None, *, options=None):
    """Stages 1-7 of a real job: pipeline -> axes -> fit -> overlay.

    Returns ``(job_dir, SynthPair, superimposed doc)``.
    """
    import pipelines
    from core import overlay
    from jobs import job_dir, load_job, save_job

    job_id, pair = fx_synth_job(opts or synth.HARD)
    pipelines.run_pipeline(job_id)
    d = job_dir(job_id)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(
        json.dumps({"prefix": PREFIX, "axes": AXES}))
    if options is not None:
        job = load_job(job_id)
        job.options = dict(options)
        save_job(job)
    fit_mod.run(job_id)
    overlay.run(job_id)
    doc = ezdxf.readfile(str(d / "out" / "superimposed.dxf"))
    return d, pair, doc


# ─────────────────────────────────────────────────────────────────────────────
# the layer scheme
# ─────────────────────────────────────────────────────────────────────────────

def test_every_entity_on_source_or_qa_layer(fx_synth_job):
    """Modelspace entities sit on MECH|*, STR|* or QA|* — nothing else.

    Runs the whole stage so the QA grid is drawn too, then checks the
    prefixing inside block definitions as well.
    """
    _, _, doc = _run_job(fx_synth_job)

    msp = doc.modelspace()
    layers = {e.dxf.layer for e in msp}
    assert layers
    bad = sorted(l for l in layers
                 if not l.startswith(("MECH|", "STR|", "QA|",)))
    assert not bad, bad
    assert any(l.startswith("MECH|") for l in layers)
    assert any(l.startswith("STR|") for l in layers)
    assert {QA_GRID_LAYER, QA_GRID_STR_LAYER, QA_OUTLIER_LAYER} <= _names(doc)

    # …and inside the block definitions too (layer "0"/"Defpoints" aside)
    for block in doc.blocks:
        if block.name.startswith(("*Model_Space", "*Paper_Space")):
            continue
        for e in block:
            if e.dxf.layer in KEEP:
                continue
            assert overlay_mod.split_source_layer(e.dxf.layer), \
                f"{block.name}: {e.dxf.layer!r} has no source prefix"

    # the originals are gone, the two special layers are not
    names = _names(doc)
    assert {"0", "Defpoints"} <= names
    assert doc.header.get("$CLAYER") == "0"


def test_source_layer_name_scheme():
    """The naming scheme itself: SOURCE_LAYER_SEP + a source tag + the name."""
    assert SOURCE_LAYER_SEP == "|"
    assert overlay_mod.source_layer("MECH", "FRAME-STEEL") == "MECH|FRAME-STEEL"
    assert overlay_mod.split_source_layer("STR|CENTER") == ("STR", "CENTER")
    assert overlay_mod.split_source_layer("SPOTCHECK_CLOUDS") is None
    assert overlay_mod.split_source_layer("MECH|") is None


def test_str_layer_names_are_stripped_of_the_xref_prefix(fx_synth_hard):
    """The xref renamed `FRAME-STEEL` to `STR$0$FRAME-STEEL`; the reader
    must see `STR|FRAME-STEEL`."""
    mech, _, report = _overlay(fx_synth_hard, style=OverlayStyle())
    renamed = report.str_layer_map["FRAME-STEEL"]
    assert renamed != "FRAME-STEEL"                     # xref did rename it
    assert report.layers.str_layers[renamed] == "STR|FRAME-STEEL"
    assert "STR|FRAME-STEEL" in _names(mech)
    assert renamed not in _names(mech)                  # the old entry is gone


def test_modelspace_layer_zero_gets_a_source_layer(fx_synth_hard):
    """An entity on "0" in modelspace becomes MECH|0 / STR|0, which is what
    keeps the blocks it inserts inheriting a source-coloured layer."""
    def prep(mech, str_doc):
        mech.modelspace().add_line((0, 0), (100, 100))            # layer 0
        str_doc.modelspace().add_line((0, 0), (0.1, 0.1))        # layer 0

    mech, _, report = _overlay(fx_synth_hard, style=OverlayStyle(), prep=prep)
    handles = set(report.str_entity_handles)
    on_zero = [e for e in mech.modelspace()
               if e.dxf.layer.endswith(SOURCE_LAYER_SEP + "0")]
    assert len(on_zero) == 2
    assert {e.dxf.layer for e in on_zero} == {"MECH|0", "STR|0"}
    assert all((e.dxf.handle in handles) == (e.dxf.layer == "STR|0") for e in on_zero)
    assert {"MECH|0", "STR|0"} <= _names(mech)
    # …and the original "0" entry is still there, untouched
    assert mech.layers.get("0").dxf.color == 7


def test_block_layer_zero_preserved():
    """"0" and "Defpoints" keep their name inside a block definition.

    An entity on layer "0" inherits the layer of the INSERT, so renaming it
    would break ByLayer/ByBlock inheritance for the whole block.
    """
    mech = ezdxf.new("R2013")
    mech.modelspace().add_line((0, 0), (1, 1))                    # layer 0
    mech.modelspace().add_line((0, 0), (2, 2), dxfattribs={"layer": "WALLS"})
    blk = mech.blocks.new("MECH-B")
    blk.add_line((0, 0), (100, 0))                               # layer 0
    blk.add_line((0, 0), (100, 100), dxfattribs={"layer": "Defpoints"})
    blk.add_line((0, 0), (50, 50), dxfattribs={"layer": "WALLS"})
    mech.modelspace().add_blockref("MECH-B", (0, 0), dxfattribs={"layer": "WALLS"})

    strd = ezdxf.new("R2013")
    strd.modelspace().add_line((0, 0), (1, 1))                    # layer 0
    sblk = strd.blocks.new("STR-B")
    sblk.add_line((0, 0), (10, 0))                               # layer 0
    sblk.add_line((0, 0), (10, 10), dxfattribs={"layer": "Defpoints"})
    sblk.add_line((0, 0), (5, 5), dxfattribs={"layer": "STEEL"})
    strd.modelspace().add_blockref("STR-B", (0, 0), dxfattribs={"layer": "STEEL"})

    report = overlay_docs(mech, strd, Affine2D(1000.0, 0.0, 0.0, 1000.0, 1e5, 2e5),
                          style=OverlayStyle())

    blocks = {b.name: {e.dxf.layer for e in b} for b in mech.blocks}
    assert blocks["MECH-B"] == {"0", "Defpoints", "MECH|WALLS"}
    str_block = report.str_block_map["STR-B"]
    assert blocks[str_block] == {"0", "Defpoints", "STR|STEEL"}
    assert report.layers.str_layers["0"] == "STR|0"      # modelspace did move
    assert report.layers.mech_layers["0"] == "MECH|0"
    assert {"MECH|WALLS", "STR|STEEL", "MECH|0", "STR|0"} <= _names(mech)


def test_unused_original_layers_are_deleted(fx_synth_hard):
    """The source layer entries go away once nothing references them."""
    mech, _, report = _overlay(fx_synth_hard, style=OverlayStyle())
    deleted = set(report.layers.deleted_layers)
    assert "CENTER" in deleted and report.str_layer_map["CENTER"] in deleted
    assert deleted <= set(report.layers.mech_layers) | set(report.layers.str_layers)
    names = _names(mech)
    assert not (deleted & names)
    assert doc_audit_clean(mech)


def doc_audit_clean(doc) -> bool:
    auditor = doc.audit()
    assert auditor.has_errors is False, [e.message for e in auditor.errors]
    return True


# ─────────────────────────────────────────────────────────────────────────────
# colour modes
# ─────────────────────────────────────────────────────────────────────────────

def test_original_mode_preserves_layer_props(fx_synth_hard):
    """color_mode="original": STR|FRAME-STEEL keeps the STR drawing's colour,
    linetype, lineweight and flags; MECH|CENTER keeps MECH's."""
    seen = {}

    def prep(mech, str_doc):
        for doc in (mech, str_doc):
            _add_linetype(doc, "DASHED")
            _add_linetype(doc, "CENTER")
        _set_props(mech, "CENTER", color=30, linetype="CENTER", lineweight=50,
                   plot=0, frozen=True)
        _set_props(str_doc, "FRAME-STEEL", color=6, linetype="DASHED",
                   lineweight=35, plot=1, locked=True)
        # snapshot: the originals are deleted by the time the test can look
        seen["mech"] = _snapshot(mech.layers.get("CENTER"))
        seen["str"] = _snapshot(str_doc.layers.get("FRAME-STEEL"))
        # an entity colour that must survive (this is not the contrast mode)
        if not str_doc.layers.has_entry("MINE"):
            str_doc.layers.new("MINE")
        line = str_doc.modelspace().add_line((1.0, 2.0), (3.0, 4.0),
                                             dxfattribs={"layer": "MINE", "color": 5})
        line.dxf.true_color = 0x00FF00

    mech, _, report = _overlay(fx_synth_hard, style=OverlayStyle(), prep=prep)

    renamed = report.str_layer_map["FRAME-STEEL"]
    dst_mech = mech.layers.get(report.layers.mech_layers["CENTER"])
    dst_str = mech.layers.get(report.layers.str_layers[renamed])
    assert dst_mech.dxf.name == "MECH|CENTER"
    assert dst_str.dxf.name == "STR|FRAME-STEEL"

    for name in ("color", "linetype", "lineweight", "plot"):
        assert getattr(dst_mech.dxf, name) == seen["mech"][name], name
        assert getattr(dst_str.dxf, name) == seen["str"][name], name
    assert dst_mech.is_frozen() is True
    assert dst_str.is_locked() is True
    # entity colours are untouched too (this is not the contrast mode)
    kept = [e for e in mech.modelspace() if e.dxf.layer == "STR|MINE"]
    assert len(kept) == 1
    assert kept[0].dxf.color == 5
    assert kept[0].dxf.true_color == 0x00FF00


def test_contrast_mode_colors(fx_synth_hard):
    """color_mode="contrast": MECH|* = 5, STR|* = 1 and every entity of a
    drawing is BYLAYER (BYBLOCK aside) with no true colour left."""
    def prep(mech, str_doc):
        # explicit colours on both drawings that must not survive
        for e in list(str_doc.modelspace())[:5]:
            e.dxf.color = 5
            e.dxf.true_color = 0x00FF00
        list(mech.modelspace())[0].dxf.color = 1
        list(mech.modelspace())[0].dxf.true_color = 0x0000FF

    style = OverlayStyle(color_mode="contrast", mech_color=5, str_color=1)
    mech, _, report = _overlay(fx_synth_hard, style=style, prep=prep)

    for name in _names(mech):
        layer = mech.layers.get(name)
        if name.startswith("MECH|"):
            assert layer.dxf.color == 5, name
            assert not layer.dxf.hasattr("true_color"), name
        elif name.startswith("STR|"):
            assert layer.dxf.color == 1, name
            assert not layer.dxf.hasattr("true_color"), name

    handles = set(report.str_entity_handles)
    for e in mech.modelspace():
        assert e.dxf.color in (0, 256), (e.dxftype(), e.dxf.color)
        assert not e.dxf.hasattr("true_color"), e.dxftype()
    assert handles
    str_blocks = set(report.str_block_map.values())
    for block in mech.blocks:
        if block.name in str_blocks:
            for e in block:
                assert e.dxf.color in (0, 256), (block.name, e.dxftype())
                assert not e.dxf.hasattr("true_color")


def test_contrast_mode_colors_are_configurable(fx_synth_hard):
    style = OverlayStyle(color_mode="contrast", mech_color=2, str_color=6)
    mech, _, _ = _overlay(fx_synth_hard, style=style)
    for name in _names(mech):
        layer = mech.layers.get(name)
        if name.startswith("MECH|"):
            assert layer.dxf.color == 2, name
        elif name.startswith("STR|"):
            assert layer.dxf.color == 6, name


def test_byblock_entities_keep_their_colour(fx_synth_hard):
    """BYBLOCK (0) is left alone in contrast mode — it means "the INSERT"."""
    def prep(mech, str_doc):
        for e in list(str_doc.modelspace())[:3]:
            e.dxf.color = 0
    mech, _, _ = _overlay(fx_synth_hard, style=OverlayStyle(color_mode="contrast"),
                          prep=prep)
    assert any(e.dxf.color == 0 for e in mech.modelspace())


# ─────────────────────────────────────────────────────────────────────────────
# lineweight / linetype
# ─────────────────────────────────────────────────────────────────────────────

def test_str_lineweight_option(fx_synth_hard):
    """``str_lineweight`` / ``str_linetype`` land on the STR|* layers only,
    in either colour mode."""
    for mode in ("original", "contrast"):
        style = OverlayStyle(color_mode=mode, str_lineweight=50,
                             str_linetype="DASHED")
        mech, _, report = _overlay(fx_synth_hard, style=style)
        str_layers = [n for n in _names(mech) if n.startswith("STR|")]
        assert str_layers
        for name in str_layers:
            layer = mech.layers.get(name)
            assert layer.dxf.lineweight == 50, (mode, name)
            assert layer.dxf.linetype == "DASHED", (mode, name)
        assert "DASHED" in {l.dxf.name for l in mech.linetypes}
        # MECH keeps its own lineweight (whatever the source said)
        for name in _names(mech):
            if name.startswith("MECH|"):
                assert mech.layers.get(name).dxf.lineweight != 50


def test_unknown_str_linetype_is_added_with_a_fallback_pattern(fx_synth_hard):
    style = OverlayStyle(str_linetype="MY-DASH")
    mech, _, report = _overlay(fx_synth_hard, style=style)
    assert "MY-DASH" in {l.dxf.name for l in mech.linetypes}
    assert any("MY-DASH" in w for w in report.layers.warnings)
    for name in _names(mech):
        if name.startswith("STR|"):
            assert mech.layers.get(name).dxf.linetype == "MY-DASH"


def test_style_is_read_from_job_options():
    assert OverlayStyle.from_options(None).to_dict() == OverlayStyle().to_dict()
    nested = OverlayStyle.from_options({"overlay": {"color_mode": "contrast",
                                                    "qa_grid": False}})
    assert nested.color_mode == "contrast" and nested.qa_grid is False
    flat = OverlayStyle.from_options({"color_mode": "contrast", "str_lineweight": 70,
                                      "str_linetype": "DASHED", "qa_grid": "false"})
    assert flat.str_lineweight == 70 and flat.str_linetype == "DASHED"
    assert flat.qa_grid is False
    # junk never breaks the stage
    junk = OverlayStyle.from_options({"color_mode": "rainbow", "str_lineweight": "x",
                                      "str_color": None})
    assert junk.color_mode == "original" and junk.str_lineweight is None


def test_job_options_reach_the_style(fx_synth_job):
    """``job.options["overlay"]`` is what run() turns into the style."""
    d, _, doc = _run_job(fx_synth_job, options={"overlay": {"color_mode": "contrast"}})
    for name in _names(doc):
        if name.startswith("STR|"):
            assert doc.layers.get(name).dxf.color == 1, name
    report = json.loads((d / "stages" / "overlay_report.json").read_text())
    assert report["style"]["color_mode"] == "contrast"
    assert report["source_layers"]["clayer"] == "0"
    assert report["qa_grid"]["mech_intersections"] == 12


# ─────────────────────────────────────────────────────────────────────────────
# the QA grid
# ─────────────────────────────────────────────────────────────────────────────

def _truth_intersections(pair):
    return [(v.x, v.y) for v in pair.intersections_mech.values()]


def test_qa_grid_circles_at_mech_intersections(fx_synth_job):
    """Every MECH intersection gets a circle, every transformed STR
    intersection an X, and the two coincide — the fit is exact."""
    d, pair, doc = _run_job(fx_synth_job)
    msp = doc.modelspace()
    want = _truth_intersections(pair)
    transform = json.loads((d / "transform.json").read_text())
    spacing = transform["median_spacing"]
    mark = 0.01 * spacing

    circles = _centers(msp, QA_GRID_LAYER, "CIRCLE")
    assert len(circles) == len(want) == 12
    assert _worst(circles, want) <= 1e-6, "QA circles are off the intersections"
    assert [e.dxf.radius for e in msp.query("CIRCLE")
            if e.dxf.layer == QA_GRID_LAYER] == pytest.approx([mark] * len(want))

    xs = _x_centers(msp, QA_GRID_STR_LAYER, size=mark)
    assert len(xs) == 12
    assert _worst(xs, want) <= 1e-6, "the STR X markers are off the intersections"

    # the gridlines themselves: 7 MECH lines + 7 mapped STR lines
    assert len([e for e in msp if e.dxf.layer == QA_GRID_LAYER
                and e.dxftype() == "LINE"]) == 7
    assert len([e for e in msp if e.dxf.layer == QA_GRID_STR_LAYER
                and e.dxftype() == "LINE"]) == 7 + 24        # 7 axes + 12 X marks

    labels = [e.dxf.text for e in msp.query("TEXT")
              if e.dxf.layer == QA_GRID_LAYER]
    assert len(labels) == 12
    assert sorted(labels) == sorted(
        f"{c['label']} r={c['residual']:.3g}" for c in transform["correspondences"])

    assert not [e for e in msp if e.dxf.layer == QA_OUTLIER_LAYER]


def test_qa_grid_is_not_drawn_when_switched_off(fx_synth_job):
    _, _, doc = _run_job(fx_synth_job, options={"overlay": {"qa_grid": False}})
    layers = {e.dxf.layer for e in doc.modelspace()}
    assert not any(l.startswith("QA|") for l in layers)
    assert not any(n.startswith("QA|") for n in _names(doc))


def test_qa_grid_marks_the_outliers(fx_synth_job):
    """A shifted STR axis is rejected by the fit and shows up on QA|OUTLIER."""
    opts = synth.with_opts(synth.HARD, shift_str_axis={"D": 300.0})
    d, _, doc = _run_job(fx_synth_job, opts)
    t = json.loads((d / "transform.json").read_text())
    assert len(t["outliers"]) == 3

    msp = doc.modelspace()
    marks = _x_centers(msp, QA_OUTLIER_LAYER,
                       size=0.01 * t["median_spacing"])
    assert len(marks) == 3
    mech_ix = json.loads((d / "grid_mech.json").read_text())["intersections"]
    for label in t["outliers"]:
        assert any(abs(x - mech_ix[label][0]) <= 1e-6 and abs(y - mech_ix[label][1]) <= 1e-6
                   for x, y in marks), label
    assert doc.layers.get(QA_OUTLIER_LAYER).dxf.color == 1


def test_qa_grid_sizes_follow_the_grid_spacing(fx_synth_hard, tmp_path):
    """Everything on the QA layers is sized from the median grid spacing."""
    mech_doc = ezdxf.readfile(str(fx_synth_hard.mech_path))
    str_doc = ezdxf.readfile(str(fx_synth_hard.str_path))
    mg = grid_mod.detect_grid(mech_doc, PREFIX, AXES)
    sg = grid_mod.detect_grid(str_doc, PREFIX, AXES)
    transform = {"matrix": _truth(fx_synth_hard).to_matrix(),
                 "median_spacing": fit_mod.median_spacing(mg),
                 "correspondences": [{"label": "1-A", "mech": [1.0, 2.0],
                                      "residual": 0.0001234, "inlier": True}],
                 "outliers": ["1-A"]}

    out = ezdxf.readfile(str(fx_synth_hard.mech_path))
    overlay_docs(out, ezdxf.readfile(str(fx_synth_hard.str_path)),
                 _truth(fx_synth_hard), style=OverlayStyle())
    summary = draw_qa_grid(out, transform,
                           fit_mod.grid_to_json(mg, PREFIX),
                           fit_mod.grid_to_json(sg, PREFIX))
    spacing = fit_mod.median_spacing(mg)
    assert spacing == pytest.approx(5500.0, rel=1e-9)
    assert summary["median_spacing"] == spacing
    msp = out.modelspace()
    radius = next(e.dxf.radius for e in msp.query("CIRCLE")
                  if e.dxf.layer == QA_GRID_LAYER)
    assert radius == pytest.approx(0.01 * spacing)
    height = next(e.dxf.height for e in msp.query("TEXT")
                  if e.dxf.layer == QA_GRID_LAYER)
    assert height == pytest.approx(0.005 * spacing)
    # the one correspondence is an outlier, so it is marked on QA|OUTLIER
    assert summary["labels"] == 1 and summary["outliers"] == 1
    assert next(e.dxf.text for e in msp.query("TEXT")
                if e.dxf.layer == QA_GRID_LAYER) == "1-A r=0.000123"
    assert len(_x_centers(msp, QA_OUTLIER_LAYER, size=0.01 * spacing)) == 1


def test_draw_qa_grid_needs_a_transform():
    doc = ezdxf.new("R2013")
    with pytest.raises(overlay_mod.OverlayError):
        draw_qa_grid(doc, {}, {}, {})


# ─────────────────────────────────────────────────────────────────────────────
# output
# ─────────────────────────────────────────────────────────────────────────────

def test_output_reopens_audits_clean_with_pipe_layers(fx_synth_hard, tmp_path):
    """The written file reopens, audits clean and keeps its layer names —
    including the `|` ones."""
    mech, _, _ = _overlay(fx_synth_hard, style=OverlayStyle())
    path = tmp_path / "superimposed.dxf"
    summary = dxfio.save(mech, str(path))
    assert summary["errors"] == []

    out = ezdxf.readfile(str(path))
    auditor = out.audit()
    assert auditor.has_errors is False, [e.message for e in auditor.errors]
    assert not auditor.fixes, [str(f) for f in auditor.fixes]

    before = _names(mech)
    assert _names(out) == before
    assert any(n.startswith("MECH|") for n in before)
    assert any(n.startswith("STR|") for n in before)

    msp = out.modelspace()
    bad = sorted({e.dxf.layer for e in msp
                  if not e.dxf.layer.startswith(("MECH|", "STR|", "QA|"))})
    assert not bad, bad
    assert out.header.get("$CLAYER") == "0"
    # geometry survived the relayering
    assert len(list(msp)) > 50


def test_overlay_report_records_the_layer_scheme(fx_synth_hard):
    _, _, report = _overlay(fx_synth_hard, style=OverlayStyle())
    payload = report.to_dict()
    assert payload["style"]["color_mode"] == "original"
    assert payload["source_layers"]["mech_layers"]["CENTER"] == "MECH|CENTER"
    assert "CENTER" in payload["source_layers"]["deleted_layers"]
    assert payload["source_layers"]["clayer"] == "0"