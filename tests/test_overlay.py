#!/usr/bin/env python3
"""test_overlay.py — core/overlay.py (PLAN.md step 6).

Every test drives :func:`core.overlay.overlay_docs` directly on the generator
output of ``tests/synth.py`` (not on cleaned files), so the overlay is
measured against exact ground truth in isolation from the cleaning stage.
"""
from __future__ import annotations

import ast
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import ezdxf
import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import geomtruth as gt          # noqa: E402
import synth                    # noqa: E402
from core import dxfio          # noqa: E402
from core import overlay        # noqa: E402
from core.overlay import (      # noqa: E402
    OverlayError, OverlayOptions, REPORT_NAME, overlay_docs, run,
)
from core.transform2d import Affine2D  # noqa: E402

# Flattening distance used for geometry comparisons. The STR files of the
# HARD preset are in metres, so 1e-4 m == 0.1 mm on the other side.
FLAT_MM = 0.1
FLAT_M = 1e-4
TRUTH = synth.truth_for(synth.HARD)          # scale 1000, rotation 7.5 degrees
AFFINE = Affine2D(*TRUTH.as_affine())


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _overlay(pair, *, affine=None, options=None):
    """(mech_doc, str_doc, report) after overlaying the pair's generator files."""
    mech = ezdxf.readfile(str(pair.mech_path))
    str_doc = ezdxf.readfile(str(pair.str_path))
    report = overlay_docs(mech, str_doc, affine or AFFINE, options=options)
    return mech, str_doc, report


def _view(doc, report, source="str"):
    """Delete every modelspace entity that does not belong to `source`.

    ``overlay_docs`` reports the handles of the STR-sourced entities it left
    in modelspace, which is what identifies them (before step 7 they share the
    layer names of the source drawings).
    """
    str_handles = set(report.str_entity_handles)
    msp = doc.modelspace()
    for e in list(msp):
        if (e.dxf.handle in str_handles) != (source == "str"):
            msp.delete_entity(e)
    return doc


def _keep_only(doc, block_name):
    """Keep only the modelspace INSERTs that reference `block_name`."""
    msp = doc.modelspace()
    for e in list(msp):
        if not (e.dxftype() == "INSERT" and e.dxf.name == block_name):
            msp.delete_entity(e)
    return doc


def _match(actual, expected, *, flat=FLAT_MM):
    ext = float(np.abs(np.vstack([p.pts for p in actual])).max())
    gt.assert_prims_match(actual, expected, gt.tolerance(ext, flat=flat))


def _extent(pts) -> float:
    a = np.asarray([[p[0], p[1]] for p in pts], dtype=float)
    return float(np.hypot(*np.ptp(a, axis=0)))


class _SkewTruth:
    """``gt.map_prims`` needs ``apply`` + ``scale``; affine, not similarity."""

    scale = 1.0
    rotation_deg = 0.0

    def __init__(self, affine: Affine2D):
        self.affine = affine

    def apply(self, xy):
        arr = np.asarray(xy, dtype=float)
        return (self.affine.apply_many(arr[None, :])[0] if arr.ndim == 1
                else self.affine.apply_many(arr))


def _no_text_metrics(prims):
    """Height/rotation are meaningless under a skew: compare position only."""
    return [replace(p, height=None, rotation=None) for p in prims]


# ─────────────────────────────────────────────────────────────────────────────
# geometry
# ─────────────────────────────────────────────────────────────────────────────

def test_str_world_geometry_matches_truth(fx_synth_hard):
    """Every STR primitive lands where the ground-truth similarity puts it."""
    mech, str_doc, report = _overlay(fx_synth_hard)
    assert report.failures == []
    assert report.dropped == 0 and report.str_entities_imported > 0

    actual = gt.world_primitives(_view(mech, report, "str"), flat=FLAT_MM)
    expected = gt.map_prims(gt.world_primitives(str_doc, flat=FLAT_M), TRUTH)
    assert len(actual) == len(expected) > 50
    _match(actual, expected)


def test_mech_geometry_unchanged(fx_synth_hard):
    """The MECH drawing is never touched — not even by 1e-12."""
    before = gt.world_primitives(ezdxf.readfile(str(fx_synth_hard.mech_path)),
                                 flat=FLAT_MM)
    mech, _, report = _overlay(fx_synth_hard)
    assert report.mech_entities == len(list(
        ezdxf.readfile(str(fx_synth_hard.mech_path)).modelspace()))

    after = gt.world_primitives(_view(mech, report, "mech"), flat=FLAT_MM)
    assert len(after) == len(before)
    gt.assert_prims_match(after, before, 1e-12)


def test_repeated_block_inserts_not_collapsed(fx_synth_hard):
    """12 INSERTs of one shared block keep their own positions."""
    mech, _, report = _overlay(fx_synth_hard)
    out = _view(mech, report, "str")
    name = report.str_block_map["Part-COL300"]
    ins = [e for e in out.modelspace() if e.dxftype() == "INSERT" and e.dxf.name == name]
    assert len(ins) == 12, "repeated INSERTs of Part-COL300 were lost"

    tol = 1e-6 * _extent(list(fx_synth_hard.intersections_mech.values()))
    hits = set()
    for key, want in fx_synth_hard.intersections_mech.items():
        d = np.hypot(*(np.array([[e.dxf.insert.x, e.dxf.insert.y] for e in ins])
                       - np.array([want.x, want.y])).T)
        assert d.min() <= tol, f"no column at intersection {key}"
        hits.add(int(d.argmin()))
    assert len(hits) == 12, "columns collapsed onto each other"
    # …and the block definition itself was not scaled into place.
    block = {b.name: b for b in out.blocks}[name]
    square = [e for e in block if e.dxftype() == "LWPOLYLINE"][0]
    assert np.ptp(np.array([[v[0], v[1]] for v in square.vertices()]), axis=0).tolist() == [300.0, 300.0]


def test_nested_mirrored_rotated_bay(fx_synth_hard):
    """A block holding a mirrored and a 45-degree INSERT of another block."""
    mech, str_doc, report = _overlay(fx_synth_hard)
    out = _view(mech, report, "str")
    bay = report.str_block_map["Part-BAY"]
    col = report.str_block_map["Part-COL300"]

    ins = [e for e in out.modelspace() if e.dxftype() == "INSERT" and e.dxf.name == bay]
    assert len(ins) == 2
    nested = {i.dxf.name for i in out.blocks.get(bay) if i.dxftype() == "INSERT"}
    assert nested == {col}

    block = {b.name: b for b in out.blocks}[bay]
    inserts = [i for i in block if i.dxftype() == "INSERT"]
    assert {i.dxf.name for i in inserts} == {col}
    # The nested composition (xscale -1 / rotation 45) is untouched, so both
    # bays keep their own mirrored and rotated columns.
    assert sorted(round(abs(i.dxf.xscale), 6) for i in inserts) == [1.0, 1.0]
    assert sorted(round(i.dxf.rotation, 6) for i in inserts) == [0.0, 45.0]
    assert sorted(i.dxf.xscale for i in inserts) == [-1.0, 1.0]

    actual = gt.world_primitives(_keep_only(out, bay), flat=FLAT_MM)
    expected = gt.map_prims(
        gt.world_primitives(_keep_only(str_doc, "Part-BAY"), flat=FLAT_M), TRUTH)
    assert len(actual) == len(expected) > 10
    _match(actual, expected)


def test_block_name_collision_no_hijack(fx_synth_hard):
    """MECH and STR both define `TAG`: neither may steal the other's block."""
    mech, _, report = _overlay(fx_synth_hard)
    str_tag = report.str_block_map["TAG"]
    assert str_tag != "TAG" and str_tag.startswith(overlay.XREF_PREFIX)

    # both INSERTs survive in the merged modelspace, one per source
    tags = [i for i in mech.modelspace().query("INSERT") if i.dxf.name.endswith("TAG")]
    assert {i.dxf.name for i in tags} == {"TAG", str_tag}
    assert len(tags) == 2

    blocks = {b.name: b for b in mech.blocks}
    assert {e.dxftype() for e in blocks["TAG"]} == {"LWPOLYLINE"}   # MECH square survives
    assert {e.dxftype() for e in blocks[str_tag]} == {"CIRCLE"}      # STR circle untouched


def _block_sig(block, name_map, layer_map):
    """Comparable signature of a block layout.

    ``owner`` is a handle into a different document and the nested name /
    layer are table references, so both follow the reported maps; everything
    else (all geometry) must be identical.
    """
    sig = []
    for e in block:
        attribs = {k: v for k, v in e.dxfattribs().items() if k != "handle"}
        attribs.pop("owner", None)
        name = attribs.pop("name", None)
        attribs["layer"] = layer_map.get(attribs["layer"], attribs["layer"])
        sig.append((e.dxftype(), name_map.get(name, name), attribs))
    return sig


def test_block_definitions_are_never_edited(fx_synth_hard):
    """Every imported block is a verbatim copy of its STR original.

    The only thing xref rewrites inside a definition are the table
    references (nested block name, layer), which have to follow the renamed
    entries — never the geometry.
    """
    mech, str_doc, report = _overlay(fx_synth_hard)
    out = {b.name: b for b in mech.blocks}
    src = {b.name: b for b in str_doc.blocks}
    checked = 0
    for src_name, new_name in report.str_block_map.items():
        if new_name not in out or src_name not in src:
            continue
        assert _block_sig(out[new_name], report.str_block_map, report.str_layer_map) == \
            _block_sig(src[src_name], report.str_block_map, report.str_layer_map), \
            f"block {src_name} was modified on import"
        checked += 1
    assert checked >= 8


# ─────────────────────────────────────────────────────────────────────────────
# entity types
# ─────────────────────────────────────────────────────────────────────────────

def _types_doc(kind) -> "ezdxf.document.Drawing":
    """A tiny STR (metre) drawing holding exactly one entity of `kind`."""
    doc = ezdxf.new("R2013")
    doc.header["$INSUNITS"] = 6
    msp = doc.modelspace()
    if kind == "CIRCLE":
        msp.add_circle((1.0, 2.0), 0.75)
    elif kind == "ARC":                       # under a mirrored + rotated INSERT
        blk = doc.blocks.new("MIRROR-ARC")
        blk.add_arc((0, 0), 0.5, 20.0, 160.0)
        msp.add_blockref("MIRROR-ARC", (5.0, 5.0),
                         dxfattribs={"xscale": -1.0, "rotation": 30.0})
    elif kind == "ELLIPSE":
        msp.add_ellipse((5.0, 1.0), major_axis=(2.0, 0.3, 0.0), ratio=0.5)
    elif kind == "LWPOLYLINE":                # closed-ish, with two bulges
        msp.add_lwpolyline([(0, 0, 0.0), (4, 0, 0.5), (4, 3, 0.0), (0, 3, -0.3)],
                           format="xyb")
    elif kind == "HATCH":                     # edge path: 2 lines + 2 arcs
        hatch = msp.add_hatch(color=2)
        edge = hatch.paths.add_edge_path()
        edge.add_line((6.0, -0.2), (6.4, -0.2))
        edge.add_arc(center=(6.4, 0.0), radius=0.2, start_angle=270.0,
                     end_angle=90.0, ccw=True)
        edge.add_line((6.4, 0.2), (6.0, 0.2))
        edge.add_arc(center=(6.0, 0.0), radius=0.2, start_angle=90.0,
                     end_angle=270.0, ccw=True)
    elif kind == "TEXT":
        msp.add_text("TAG-X", dxfattribs={"insert": (1.0, 6.0, 0.0),
                                          "height": 0.25, "rotation": 20.0})
    elif kind == "SPLINE":
        msp.add_spline(fit_points=[(6, 0), (7, 1), (8, -1), (9, 0)])
    else:                                          # pragma: no cover
        raise AssertionError(kind)
    return doc


@pytest.mark.parametrize("kind", ["CIRCLE", "ARC", "ELLIPSE", "LWPOLYLINE",
                                  "HATCH", "TEXT", "SPLINE"])
def test_entity_types_transform(kind, tmp_path):
    mech = ezdxf.new("R2013")
    mech.modelspace().add_line((0, 0), (1, 1))            # one MECH entity
    str_doc = _types_doc(kind)
    report = overlay_docs(mech, str_doc, AFFINE)
    assert report.failures == [], report.failures

    if kind == "ARC":                                    # mirror kept, not baked
        ins = [e for e in mech.modelspace() if e.dxftype() == "INSERT"]
        assert len(ins) == 1
        # the matrix is composed into the INSERT: mirror * scale, rot + 7.5
        assert ins[0].dxf.xscale == pytest.approx(-TRUTH.scale, abs=1e-6)
        assert ins[0].dxf.xscale < 0                    # still mirrored
        assert ins[0].dxf.rotation == pytest.approx(37.5, abs=1e-9)

    out = _view(mech, report, "str")
    actual = gt.world_primitives(out, flat=FLAT_MM)
    expected = gt.map_prims(gt.world_primitives(str_doc, flat=FLAT_M), TRUTH)
    assert len(actual) == len(expected) == 1
    _match(actual, expected)

    if kind == "TEXT":                       # height x1000, rotation +7.5 degrees
        assert actual[0].kind == "TEXT"
        assert actual[0].height == pytest.approx(0.25 * TRUTH.scale, rel=1e-9)
        assert actual[0].rotation == pytest.approx((20.0 + TRUTH.rotation_deg) % 360.0,
                                                   abs=1e-6)


# ─────────────────────────────────────────────────────────────────────────────
# fallbacks, output, plumbing
# ─────────────────────────────────────────────────────────────────────────────

def test_affine_skew_triggers_logged_fallback(fx_synth_hard):
    """A skew cannot be represented by an INSERT: explode it, don't lose it."""
    a, b, c, d, tx, ty = TRUTH.as_affine()
    skew = Affine2D(a, b + 0.25 * d, c, d, tx, ty)
    mech, str_doc, report = _overlay(fx_synth_hard, affine=skew)

    assert report.failures, "a skewed matrix produced no failures at all"
    assert report.dropped == 0, report.failures
    assert all(f["action"] == "exploded" for f in report.failures)
    assert report.str_entities_imported > report.str_entities_total

    out = _view(mech, report, "str")
    assert not [e for e in out.modelspace() if e.dxftype() == "INSERT"], \
        "an untransformed STR INSERT survived the fallback"
    actual = _no_text_metrics(gt.world_primitives(out, flat=FLAT_MM))
    expected = _no_text_metrics(gt.map_prims(
        gt.world_primitives(str_doc, flat=FLAT_M), _SkewTruth(skew)))
    _match(actual, expected)


def test_dropped_entities_fail_the_stage(fx_synth_hard):
    """max_drop_ratio 0.0 (the similarity default) turns one drop into an error."""
    mech = ezdxf.readfile(str(fx_synth_hard.mech_path))
    str_doc = ezdxf.readfile(str(fx_synth_hard.str_path))
    with pytest.raises(OverlayError, match="dropped"):
        overlay_docs(mech, str_doc, AFFINE, options=OverlayOptions(max_drop_ratio=-1.0))
    assert OverlayOptions.for_model("similarity").max_drop_ratio == 0.0
    assert OverlayOptions.for_model("affine").max_drop_ratio == 0.01


def test_overlay_output_audits_clean(fx_synth_hard, tmp_path):
    mech, _, report = _overlay(fx_synth_hard)
    path = tmp_path / "superimposed.dxf"
    summary = dxfio.save(mech, str(path))
    assert summary["errors"] == []

    out = ezdxf.readfile(str(path))
    auditor = out.audit()
    assert auditor.has_errors is False, [e.message for e in auditor.errors]
    # the written file round-trips into the same picture
    assert len(gt.world_primitives(out, flat=FLAT_MM)) > 100


def test_run_writes_output_and_report(fx_synth_job):
    """Stage entrypoint: roles -> dxfio -> out/superimposed.dxf + report."""
    from jobs import job_dir
    job_id, pair = fx_synth_job(synth.HARD)
    d = job_dir(job_id)
    (d / "cleaned").mkdir(parents=True, exist_ok=True)
    for src, dst in ((pair.mech_path, "zz_plant_clean.dxf"),
                     (pair.str_path, "aa_frame_clean.dxf")):
        shutil.copyfile(src, d / "cleaned" / dst)
    (d / "transform.json").write_text(json.dumps(AFFINE.to_json()))

    run(job_id)

    out = d / "out" / "superimposed.dxf"
    assert out.exists()
    assert ezdxf.readfile(str(out)).audit().has_errors is False

    report = json.loads((d / "stages" / REPORT_NAME).read_text())
    assert report["failures"] == []
    assert report["str_entities_imported"] == len(report["str_entity_handles"])
    assert report["str_block_map"]["Part-COL300"].startswith(overlay.XREF_PREFIX)
    assert report["matrix"] == AFFINE.to_matrix()
    assert report["affine"]["scale"] == pytest.approx(TRUTH.scale)
    audit = json.loads((d / "stages" / "audit.json").read_text())
    assert any(k.startswith("overlay:") for k in audit)


def test_no_bare_except_in_overlay():
    """PLAN.md step 6: no `except: pass` anywhere in the module."""
    tree = ast.parse((ROOT / "core" / "overlay.py").read_text())
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "the module has no except clause at all"
    for node in handlers:
        body = [n for n in node.body
                if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        assert body, f"bare except at line {node.lineno}: {ast.dump(node.body[0])}"
        assert node.type is not None or node.name is not None, \
            f"bare except at line {node.lineno}"