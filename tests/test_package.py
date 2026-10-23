#!/usr/bin/env python3
"""test_package.py — core/package.py step 8.

The preview is drawn on the **full** extents (nothing is cropped away),
geometry far outside the grid box is *reported* instead of hidden, dangling
INSERTs are counted instead of silently dropped, and ``report.json`` carries
the roles, the counts, the fit quality, the overlay failures and the audit.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import ezdxf
import pytest
from ezdxf import bbox as ez_bbox
from ezdxf import disassemble, recover

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import synth                                  # noqa: E402
from core import package as package_mod        # noqa: E402
from core.overlay import split_source_layer    # noqa: E402

PREFIX = "TP104-"
AXES = ["A", "B", "C", "D", "1", "2", "3"]
MIN_COLOURS = 3      # a rendered drawing; a failed render writes 1-colour white


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _run_job(fx_synth_job, opts=None, *, options=None):
    """Stages 1-8 of a real job: pipeline → axes → fit → overlay → package.

    Returns ``(job_dir, SynthPair, report)``.
    """
    import pipelines
    from core import fit, overlay
    from jobs import job_dir, load_job, save_job

    job_id, pair = fx_synth_job(opts or synth.HARD)
    pipelines.run_pipeline(job_id)
    d = job_dir(job_id)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": PREFIX, "axes": AXES}))
    if options is not None:
        job = load_job(job_id)
        job.options = dict(options)
        save_job(job)
    fit.run(job_id)
    overlay.run(job_id)
    package_mod.run(job_id)
    report = json.loads((d / "out" / "report.json").read_text())
    return d, pair, report


def _merged(pair):
    """The generator files after stage 6, in memory (truth fit, no layers)."""
    from core.overlay import overlay_docs
    from core.transform2d import Affine2D
    mech = ezdxf.readfile(str(pair.mech_path))
    overlay_docs(mech, ezdxf.readfile(str(pair.str_path)),
                 Affine2D(*pair.truth.as_affine()))
    return mech


def _decomposed_count(doc) -> int:
    return sum(1 for _ in disassemble.recursive_decompose(doc.modelspace()))


def _world_bbox(entities) -> list | None:
    acc = [math.inf, math.inf, -math.inf, -math.inf]
    n = 0
    for e in entities:
        for bb in ez_bbox.multi_flat([e]):
            acc = [min(acc[0], bb.extmin[0]), min(acc[1], bb.extmin[1]),
                   max(acc[2], bb.extmax[0]), max(acc[3], bb.extmax[1])]
            n += 1
    return acc if n else None


# ─────────────────────────────────────────────────────────────────────────────
# the preview shows everything
# ─────────────────────────────────────────────────────────────────────────────

def _add_stray(dxf_path: Path, at) -> str:
    """Put a STR-sourced CIRCLE far outside the grid box into the output.

    That is how a stray reaches the superimposed file in production — the
    transform put a piece in the wrong place — and not by being in the upload:
    an entity 1e7 away in the *input* inflates the drawing extent so much that
    step 4's relative grid-line threshold (0.15 × extent) matches nothing and
    the fit never runs.  Returns the new handle.
    """
    from core import dxfio
    doc = ezdxf.readfile(str(dxf_path))
    if not doc.layers.has_entry("STR|STRAY"):
        doc.layers.new("STR|STRAY")
    circle = doc.modelspace().add_circle(at, 1.0, dxfattribs={"layer": "STR|STRAY"})
    handle = circle.dxf.handle
    dxfio.save(doc, str(dxf_path))
    return handle


def test_preview_extents_include_stray(fx_synth_job, fx_synth_hard, tmp_path):
    """A stray STR entity at (1e7, 1e7) stays in the picture *and* is named.

    The percentile/IQR crop this replaced made such an entity vanish from the
    preview.  Now the full extents cover it and ``report.outliers`` names the
    entity that causes it.
    """
    # (1) the generator fixture, straight through the overlay (the step-0
    #     xfail body): the extents contain the transformed stray point.
    opts = synth.with_opts(fx_synth_hard.opts, stray_str_entity_at=(1.0e7, 1.0e7))
    pair = synth.build_pair(tmp_path / "stray", opts)
    world = pair.truth.apply((1.0e7, 1.0e7))
    xmin, ymin, xmax, ymax = package_mod._compute_extents(_merged(pair))
    assert xmin <= world[0] <= xmax and ymin <= world[1] <= ymax, \
        ((xmin, ymin, xmax, ymax), world)
    assert xmax - xmin > 0.9 * 1.0e7

    # (2) a real stage-8 run: reported, not hidden.
    stray_at = (1.0e7, 1.0e7)
    d, _, report = _run_job(fx_synth_job)
    dxf_path = d / "out" / "superimposed.dxf"
    handle = _add_stray(dxf_path, stray_at)
    package_mod.run(report["job_id"])
    report = json.loads((d / "out" / "report.json").read_text())

    rows = [o for o in report["outliers"] if o["handle"] == handle]
    assert len(rows) == 1, report["outliers"]
    row = rows[0]
    assert row["layer"] == "STR|STRAY" and row["type"] == "CIRCLE"
    assert row["source"] == "STR"
    assert row["bbox"][0] <= stray_at[0] <= row["bbox"][2]
    assert row["bbox"][1] <= stray_at[1] <= row["bbox"][3]
    assert row["grid_diagonals"] > package_mod.OUTLIER_DIAGONALS
    assert row["fraction_of_extents"] > 0.5
    assert report["outliers_total"] == len(report["outliers"])

    # the extents the report publishes still cover the stray
    ex = report["extents"]["full"]
    assert ex[0] <= stray_at[0] <= ex[2] and ex[1] <= stray_at[1] <= ex[3], (ex,)

    # and the preview was rendered on them, not on the old crop
    meta = json.loads((d / "out" / "preview_meta.json").read_text())
    assert meta["world_extents"] == ex


def test_clean_drawing_has_no_outliers_and_qa_is_excluded(fx_synth_job):
    """Nothing is reported for a good drawing, and the QA grid this stage
    draws cannot widen the box it is measured against."""
    d, _, report = _run_job(fx_synth_job)
    assert report["outliers"] == []
    assert report["outliers_total"] == 0

    doc = ezdxf.readfile(str(d / "out" / "superimposed.dxf"))
    qa_box = _world_bbox([e for e in doc.modelspace()
                          if (split_source_layer(e.dxf.layer) or ("", ""))[0] == "QA"])
    assert qa_box is not None, "no QA geometry in the output"
    grid = report["extents"]["grid"]
    assert grid[0] <= qa_box[0] and grid[1] <= qa_box[1], \
        "QA geometry widened the grid box"
    assert grid[2] >= qa_box[2] and grid[3] >= qa_box[3]


def test_preview_png_nonblank(fx_synth_job):
    """Both previews carry drawing pixels (a failed render is caught)."""
    from PIL import Image
    d, _, _ = _run_job(fx_synth_job)
    for name in ("preview.png", "preview_grid.png"):
        path = d / "out" / name
        assert path.exists(), name
        im = Image.open(path).convert("RGB")
        assert im.size == (1000, 600), (name, im.size)
        colours = len(set(im.getdata()))
        assert colours > MIN_COLOURS, f"{name} looks blank ({colours} colours)"


def test_preview_meta_maps_world_to_pixels(fx_synth_job):
    """``preview_meta.json`` turns a world coordinate into a PNG pixel.

    Two opposite corners of the world extents map to two opposite corners of
    the drawn axes box, both inside the image.
    """
    d, _, report = _run_job(fx_synth_job)
    meta = json.loads((d / "out" / "preview_meta.json").read_text())
    assert set(meta) == {"png_w", "png_h", "world_to_px", "world_extents"}
    assert (meta["png_w"], meta["png_h"]) == (1000, 600)
    assert meta["world_extents"] == report["extents"]["full"]

    (sx, _, tx), (_, sy, ty) = meta["world_to_px"]
    x0, y0, x1, y1 = meta["world_extents"]
    px0, py0 = sx * x0 + tx, sy * y0 + ty
    px1, py1 = sx * x1 + tx, sy * y1 + ty
    assert px0 != px1 and py0 != py1, "degenerate world_to_px"
    for px, py in ((px0, py0), (px1, py1)):
        assert 0.0 <= px <= meta["png_w"]
        assert 0.0 <= py <= meta["png_h"]
    # a PNG counts y from the top, so the world y axis is flipped
    assert sx > 0.0 and sy < 0.0


def test_preview_recolors_in_memory_only(fx_synth_job):
    """MECH grey / STR red / QA green for the picture; the DXF on disk keeps
    the colours stage 7 wrote."""
    d, _, _ = _run_job(fx_synth_job)
    dxf_path = d / "out" / "superimposed.dxf"

    doc = ezdxf.readfile(str(dxf_path))
    on_disk = {l.dxf.name: l.dxf.color for l in doc.layers}
    recolored = package_mod._recolor_for_preview(doc)
    colours = recolored["colors"]
    assert colours, "no layer matched MECH| / STR| / QA|GRID*"

    for name, aci in colours.items():
        assert doc.layers.get(name).dxf.color == aci, name
        if name.startswith("QA|"):
            assert aci == 3, name
        else:
            assert aci == (8 if name.startswith("MECH|") else 1), name
    assert not any(n.startswith(package_mod.MISMATCH_PREFIX) for n in colours)
    # step 8b's per-severity QA|MISMATCH* layers keep their own colours
    for layer in doc.layers:
        if layer.dxf.name.startswith(package_mod.MISMATCH_PREFIX):
            assert doc.layers.get(layer.dxf.name).dxf.color == layer.dxf.color

    # rendering the preview does not write the recoloured document back
    package_mod._render_preview(dxf_path, d / "out" / "preview.png")
    assert {l.dxf.name: l.dxf.color
            for l in ezdxf.readfile(str(dxf_path)).layers} == on_disk


# ─────────────────────────────────────────────────────────────────────────────
# the report
# ─────────────────────────────────────────────────────────────────────────────

def test_report_counts_consistent(fx_synth_job):
    """``str_out == str_in`` — every STR primitive survived the overlay.

    "in" counts the cleaned pair the overlay consumed, "out" the primitives of
    the superimposed file grouped by layer prefix (after recursive
    decomposition — most STR geometry lives inside INSERTs, on layer ``0``).
    """
    d, _, report = _run_job(fx_synth_job)
    counts = report["counts"]
    assert counts["mech_in"] and counts["str_in"] > 0
    assert counts["str_out"] == counts["str_in"], counts
    assert counts["mech_out"] == counts["mech_in"], counts
    assert report["overlay"]["failures"] == []
    assert counts["qa_out"] > 0, "the QA grid was not counted"

    # a hand-rolled decomposition of the two cleaned files agrees
    from core.roles import resolve_pair
    mech_path, str_path = resolve_pair(report["job_id"])
    for path, key in ((mech_path, "mech_in"), (str_path, "str_in")):
        assert _decomposed_count(ezdxf.readfile(str(path))) == counts[key], path

    # by_type_source / by_layer_source partition the same totals
    for src, key in (("MECH", "mech_out"), ("STR", "str_out"), ("QA", "qa_out")):
        assert sum(counts["by_type_source"][src].values()) == counts[key]
        assert sum(counts["by_layer_source"][src].values()) == counts[key]
    assert any(k.startswith("STR|") for k in counts["by_layer_source"]["STR"])


def test_report_includes_fit_quality_and_failures(fx_synth_job):
    """Everything step 9's UI needs is in report.json."""
    d, pair, report = _run_job(fx_synth_job)

    fit = report["fit"]
    assert fit["status"] == "ok", fit
    assert fit["scale"] == pytest.approx(pair.truth.scale, rel=1e-9)
    assert fit["rotation_deg"] == pytest.approx(pair.truth.rotation_deg, abs=1e-9)
    assert fit["rms_residual"] is not None and fit["loo_max"] is not None
    assert fit["outliers"] == []
    assert 0 < fit["n_inliers"] <= fit["n_total"]

    ov = report["overlay"]
    assert ov["failures"] == []
    assert ov["dropped"] == 0 and ov["exploded"] == 0
    assert "renamed_resources" in ov
    assert ov["import_method"] == "xref"
    assert ov["qa_grid"]["mech_intersections"] == 12

    # roles come from the manifest, not from the filenames
    roles = {r["role"]: Path(r["cleaned"]).name for r in report["roles"]}
    assert roles["mech"] == "zz_plant_clean.dxf"
    assert roles["str"] == "aa_frame_clean.dxf"

    # the legacy transform keys stay for app.js / analyze.py
    t = report["transform"]
    for key in ("scale_x", "scale_y", "tx", "ty", "x_labels", "y_labels",
                "rms_residual", "max_residual", "matrix", "model"):
        assert key in t, key
    assert t["tx"] == pytest.approx(pair.truth.tx, abs=1e-6)

    # audit trail + preview artifacts + extents
    assert report["audit"], "stages/audit.json missing from the report"
    stages = {k.split(":", 1)[0] for k in report["audit"]}
    assert {"convert", "clean", "overlay"} <= stages, report["audit"]
    names = {img["name"] for img in report["preview"]["images"]}
    assert names == {"preview.png", "preview_grid.png"}
    assert report["preview"]["meta"] == "out/preview_meta.json"

    ext = report["extents"]
    assert set(ext) == {"full", "mech", "str", "grid"}
    for key, box in ext.items():
        assert box is not None and box[2] > box[0] and box[3] > box[1], key
    assert report["dangling_inserts_dropped"] == 0
    assert report["dangling_inserts"] == []


def test_dangling_inserts_zero_on_synth(fx_synth_job):
    """A stage-7 output has none — and if one appeared it would be reported."""
    d, _, report = _run_job(fx_synth_job)
    assert report["dangling_inserts_dropped"] == 0
    assert report["dangling_inserts"] == []

    # the mechanism: break a reference and it shows up in the report
    doc = ezdxf.readfile(str(d / "out" / "superimposed.dxf"))
    inserts = [e for e in doc.modelspace() if e.dxftype() == "INSERT"]
    assert inserts
    handle = inserts[0].dxf.handle
    inserts[0].dxf.name = "GONE-BLOCK"
    dropped = package_mod._drop_dangling_inserts(doc)
    assert len(dropped) == 1
    assert dropped[0]["block"] == "GONE-BLOCK"
    assert dropped[0]["handle"] == handle


def test_recover_and_readfile_agree_on_superimposed(fx_synth_job):
    """``recover.readfile`` no longer loses the STR blocks.

    This is what used to force ``core/package.py`` to bypass
    ``dxfio.read_internal``; the two agree now, so the pipeline helper is used.
    """
    d, _, _ = _run_job(fx_synth_job)
    path = d / "out" / "superimposed.dxf"

    strict = ezdxf.readfile(str(path))
    recovered, _auditor = recover.readfile(str(path))

    def census(doc):
        out = {b.name: len(list(b)) for b in doc.blocks}
        out["__msp__"] = len(list(doc.modelspace()))
        out["__all_blocks__"] = sum(
            v for k, v in out.items() if not k.startswith("__"))
        return out

    assert census(recovered) == census(strict)

    # and the entities that used to disappear are still there
    assert len(list(strict.modelspace())) > 50
    layers = {l.dxf.name for l in strict.layers}
    for layer in ("MECH|CENTER", "STR|FRAME-STEEL", "QA|GRID"):
        assert layer in layers, (layer, sorted(layers))