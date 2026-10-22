#!/usr/bin/env python3
"""test_known_bugs.py — one strict xfail per verified defect (see PLAN.md).

Each test has the body that must pass once its target step is done. The
step that fixes a bug REMOVES the xfail marker. strict=True turns an
accidental fix into a failure so markers cannot go stale.

Imports of modules/APIs that do not exist yet happen inside the tests.
"""
from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

import ezdxf
import numpy as np
import pytest
from ezdxf import disassemble

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import geomtruth as gt  # noqa: E402
import synth  # noqa: E402

PREFIX = "TP104-"
SUFFIXES = ["A", "B", "C", "D", "1", "2", "3"]


def _extent(pts) -> float:
    a = np.asarray(pts, dtype=float)
    return float(np.hypot(*np.ptp(a, axis=0)))


def _decomposed(doc, pred=lambda l: True):
    return [e for e in disassemble.recursive_decompose(doc.modelspace()) if pred(e.dxf.layer)]


def _overlay(pair):
    """Planned step-6 API: overlay_docs(mech_doc, str_doc, affine) -> report.

    The result is the MECH doc, mutated in place.
    """
    from core.overlay import overlay_docs          # noqa: F401  (step 6)
    from core.transform2d import Affine2D          # noqa: F401  (step 5)
    mech = ezdxf.readfile(str(pair.mech_path))
    strd = ezdxf.readfile(str(pair.str_path))
    overlay_docs(mech, strd, Affine2D(*pair.truth.as_affine()))
    return mech


# ---------------------------------------------------------------- step 1

def test_roles_not_inferred_from_filename(fx_synth_job):
    """MECH=zz_plant.dxf (sorts last), STR=aa_frame.dxf (sorts first)."""
    import pipelines
    from core import fit
    from jobs import job_dir, load_job

    job_id, pair = fx_synth_job(synth.LEGACY_COMPATIBLE)
    pipelines.run_pipeline(job_id)                      # stages 1-6, synchronous
    job = load_job(job_id)
    assert job.state == "awaiting_axes", (job.state, job.error)
    d = job_dir(job_id)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(
        json.dumps({"prefix": PREFIX, "axes": SUFFIXES}))
    fit.run(job_id)
    t = json.loads((d / "transform.json").read_text())
    assert t["tx"] == pytest.approx(pair.truth.tx, abs=1e-6)
    assert t["ty"] == pytest.approx(pair.truth.ty, abs=1e-6)
    assert t["scale_x"] == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------- step 3

def _clean(src: Path, out: Path, *, is_str: bool):
    import core.clean_v1 as clean_v1
    clean_v1._clean_one(src, out, is_str=is_str)
    return ezdxf.readfile(str(out))


def test_clean_keeps_gusset_triangles_and_frame_layers(fx_synth_hard, tmp_path):
    src = ezdxf.readfile(str(fx_synth_hard.str_path))
    before = {l: len(_decomposed(src, lambda x, l=l: x == l))
              for l in ("FRAME-STEEL", "CIVIL-FOUNDATION")}
    assert all(before.values())
    out = _clean(fx_synth_hard.str_path, tmp_path / "str_clean.dxf", is_str=True)
    gus = _decomposed(out)
    tri = [e for e in gus if e.dxftype() == "LWPOLYLINE" and e.dxf.layer == synth.LAYER_PLATE
           and len(e) == 3]
    solid = [e for e in gus if e.dxftype() == "SOLID" and e.dxf.layer == synth.LAYER_FILL]
    assert len(tri) == 1, "3-vertex gusset polyline was deleted"
    assert len(solid) == 1, "gusset SOLID was deleted"
    for layer, n in before.items():
        assert len(_decomposed(out, lambda x, layer=layer: x == layer)) == n, layer


def test_mtext_grid_label_survives_clean(fx_synth_hard, tmp_path):
    out = _clean(fx_synth_hard.mech_path, tmp_path / "mech_clean.dxf", is_str=False)
    mtexts = [e.plain_text().strip() for e in out.modelspace().query("MTEXT")]
    assert f"{PREFIX}{fx_synth_hard.opts.mtext_label}" in mtexts


# ---------------------------------------------------------------- step 4

def test_grid_detection_rotated_inserts(fx_synth_hard):
    from core import grid
    doc = ezdxf.readfile(str(fx_synth_hard.str_path))
    res = grid.detect_grid(doc, PREFIX, SUFFIXES)
    pts = [p for p, _ in fx_synth_hard.grid_str.values()]
    extent = _extent(pts)
    for lab, (pt, d) in fx_synth_hard.grid_str.items():
        ax = res.axes[lab]
        assert ax.status == "ok", (lab, ax.status)
        n = np.array([-ax.dir[1], ax.dir[0]]) / np.hypot(ax.dir[0], ax.dir[1])
        for q in (pt, pt + d * 1000.0 * fx_synth_hard.truth.scale ** -1):
            dist = abs(float(n @ (np.array([q[0], q[1]]) - np.array([ax.point[0], ax.point[1]]))))
            assert dist <= 1e-6 * extent, (lab, dist)


# ---------------------------------------------------------------- step 5

def test_fit_recovers_rotation_and_units(fx_synth_hard):
    """Step 5. The fit is a similarity, so a rotated + metre-scaled STR
    drawing maps onto the MECH grid exactly (previously unrecoverable)."""
    from core import grid
    from core.transform2d import fit_similarity
    mdoc = ezdxf.readfile(str(fx_synth_hard.mech_path))
    sdoc = ezdxf.readfile(str(fx_synth_hard.str_path))
    im = grid.intersections(grid.detect_grid(mdoc, PREFIX, SUFFIXES))
    isr = grid.intersections(grid.detect_grid(sdoc, PREFIX, SUFFIXES))
    keys = sorted(set(im) & set(isr))
    assert len(keys) == 12
    src = np.array([[isr[k][0], isr[k][1]] for k in keys])
    dst = np.array([[im[k][0], im[k][1]] for k in keys])
    aff = fit_similarity(src, dst)
    scale, rot, mirrored, skew = aff.decompose()
    t = fx_synth_hard.truth
    assert scale == pytest.approx(t.scale, rel=1e-9)
    assert rot == pytest.approx(t.rotation_deg, abs=1e-9)
    assert not mirrored


# ---------------------------------------------------------------- step 6

def test_overlay_repeated_blocks_not_collapsed(fx_synth_hard):
    """Step 6 — fixed by core.overlay.overlay_docs: xref import keeps the
    INSERTs, transform.inplace composes into each of them separately."""
    out = _overlay(fx_synth_hard)
    prims = gt.world_primitives(out, layer_pred=lambda l: l.endswith(synth.LAYER_COLUMN))
    centers = []
    for p in prims:
        if p.kind != "CURVE":
            continue
        size = np.ptp(p.pts, axis=0)
        if abs(size[0] - 300.0) < 1.0 and abs(size[1] - 300.0) < 1.0:
            centers.append((p.pts.min(axis=0) + p.pts.max(axis=0)) / 2.0)
    centers = np.array(centers)
    tol = 1e-6 * _extent(list(fx_synth_hard.intersections_mech.values()))
    hits = set()
    for k, mpt in fx_synth_hard.intersections_mech.items():
        d = np.hypot(*(centers - np.array([mpt[0], mpt[1]])).T)
        assert d.min() <= tol, f"no column at intersection {k}"
        hits.add(int(d.argmin()))
    assert len(hits) == 12, "columns collapsed onto each other"


def test_overlay_no_mech_block_hijack(fx_synth_hard):
    """Step 6 — fixed by core.overlay.overlay_docs: XREF_PREFIX renames the
    STR `TAG` block instead of overwriting the MECH one."""
    out = _overlay(fx_synth_hard)
    tags = [i for i in out.modelspace().query("INSERT") if i.dxf.name.endswith("TAG")]
    assert len(tags) == 2
    mech_tag = [i for i in tags if i.dxf.name == "TAG"]
    str_tag = [i for i in tags if i.dxf.name != "TAG"]
    assert len(mech_tag) == 1 and len(str_tag) == 1
    kinds = {e.dxftype() for e in out.blocks.get("TAG")}
    assert kinds == {"LWPOLYLINE"}, f"MECH TAG block was modified: {kinds}"
    str_kinds = {e.dxftype() for e in out.blocks.get(str_tag[0].dxf.name)}
    assert str_kinds == {"CIRCLE"}


# ---------------------------------------------------------------- step 7

def test_overlay_source_layers(fx_synth_hard):
    """Step 7 — fixed by core.overlay.apply_source_layers: once the two
    drawings are superimposed every modelspace entity says which drawing it
    came from, so the overlay can be taken apart again in a CAD viewer."""
    from core import overlay
    from core.transform2d import Affine2D
    mech = ezdxf.readfile(str(fx_synth_hard.mech_path))
    strd = ezdxf.readfile(str(fx_synth_hard.str_path))
    report = overlay.overlay_docs(mech, strd, Affine2D(*fx_synth_hard.truth.as_affine()))
    overlay.apply_source_layers(mech, report)

    layers = {e.dxf.layer for e in mech.modelspace()}
    assert layers
    bad = [l for l in layers if not l.startswith(("MECH|", "STR|", "QA|"))]
    assert not bad, bad
    assert any(l.startswith("MECH|") for l in layers) and any(l.startswith("STR|") for l in layers)
    # the two special layers are never renamed away, and the file opens on 0
    names = {l.dxf.name for l in mech.layers}
    assert {"0", "Defpoints"} <= names
    assert mech.header.get("$CLAYER") == "0"


# ---------------------------------------------------------------- step 8

@pytest.mark.xfail(strict=True, reason="step 8: preview extents percentile-crop hides stray geometry")
def test_preview_extents_include_stray(tmp_path):
    from core import package
    opts = synth.with_opts(synth.LEGACY_COMPATIBLE, stray_str_entity_at=(1.0e7, 1.0e7))
    pair = synth.build_pair(tmp_path, opts)
    out = _overlay(pair)
    sx, sy = pair.truth.apply((1.0e7, 1.0e7))
    xmin, ymin, xmax, ymax = package._compute_extents(out)
    assert xmin <= sx <= xmax and ymin <= sy <= ymax, ((xmin, ymin, xmax, ymax), (sx, sy))


# ---------------------------------------------------------------- step 10

def _poll(client, job_id, want, timeout=60.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = client.get(f"/api/v1/overlay/{job_id}/status").json()
        if st["state"] == want:
            return st
        assert st["state"] != "failed" and not st.get("error"), st
        time.sleep(0.1)
    raise AssertionError(f"timeout waiting for {want!r}; last={st}")


def _only_layers(path_or_text, prefix, *, text=False):
    doc = ezdxf.read(io.StringIO(path_or_text)) if text else ezdxf.readfile(str(path_or_text))
    msp = doc.modelspace()
    for e in list(msp):
        if not e.dxf.layer.startswith(prefix):
            msp.delete_entity(e)
    return doc


@pytest.mark.slow
@pytest.mark.xfail(strict=True, reason="step 10: end-to-end synthetic pipeline")
def test_e2e_synthetic(fx_isolated_jobs, fx_synth_hard):
    from fastapi.testclient import TestClient
    from app import app
    from core import grid
    from jobs import job_dir

    pair = fx_synth_hard
    client = TestClient(app)
    with open(pair.mech_path, "rb") as fa, open(pair.str_path, "rb") as fb:
        r = client.post("/api/v1/overlay",
                        files={"drawing_a": (pair.mech_path.name, fa, "application/dxf"),
                               "drawing_b": (pair.str_path.name, fb, "application/dxf")},
                        data={"color_mode": "contrast"})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    _poll(client, jid, "awaiting_axes")
    assert PREFIX in json.dumps(client.get(f"/api/v1/overlay/{jid}/axes").json())
    r = client.post(f"/api/v1/overlay/{jid}/axes", json={"prefix": PREFIX, "axes": SUFFIXES})
    assert r.json()["all_matched"] is True
    assert client.post(f"/api/v1/overlay/{jid}/axes/confirm").status_code == 200
    _poll(client, jid, "done")

    dl = client.get(f"/api/v1/overlay/{jid}/download")
    assert dl.status_code == 200
    out = ezdxf.read(io.StringIO(dl.text))

    # (a) grid of STR|* content coincides with grid of MECH|*
    gm = grid.intersections(grid.detect_grid(_only_layers(dl.text, "MECH|", text=True), PREFIX, SUFFIXES))
    gs = grid.intersections(grid.detect_grid(_only_layers(dl.text, "STR|", text=True), PREFIX, SUFFIXES))
    diag = _extent(list(pair.intersections_mech.values()))
    shared = set(gm) & set(gs)
    assert len(shared) == 12
    for k in shared:
        assert np.hypot(gm[k][0] - gs[k][0], gm[k][1] - gs[k][1]) <= 1e-6 * diag

    # (b) STR content == cleaned STR mapped by truth; (c) MECH content == cleaned MECH
    cdir = job_dir(jid) / "cleaned"
    str_clean = ezdxf.readfile(str(cdir / "aa_frame_clean.dxf"))
    mech_clean = ezdxf.readfile(str(cdir / "zz_plant_clean.dxf"))
    ext = float(np.abs(np.vstack([p.pts for p in gt.world_primitives(out)])).max())
    gt.assert_prims_match(
        gt.world_primitives(out, layer_pred=lambda l: l.startswith("STR|"), flat=gt.FLAT_MM),
        gt.map_prims(gt.world_primitives(str_clean), pair.truth), gt.tolerance(ext))
    gt.assert_prims_match(
        gt.world_primitives(out, layer_pred=lambda l: l.startswith("MECH|"), flat=gt.FLAT_MM),
        gt.world_primitives(mech_clean, flat=gt.FLAT_MM), gt.tolerance(ext))

    # (d) transform.json
    t = json.loads((job_dir(jid) / "transform.json").read_text())
    assert abs(t["rotation_deg"] - pair.truth.rotation_deg) < 1e-9
    assert abs(t["scale"] - pair.truth.scale) / pair.truth.scale < 1e-9
    assert t["quality"]["status"] == "ok" and t["outliers"] == []

    # (e) report
    rep = client.get(f"/api/v1/overlay/{jid}/report.json").json()
    assert rep["overlay"]["failures"] == []
    assert rep["counts"]["str_out"] == rep["counts"]["str_in"]
    assert rep["dangling_inserts_dropped"] == 0

    # (f) preview
    pv = client.get(f"/api/v1/overlay/{jid}/preview.png")
    assert pv.status_code == 200
    from PIL import Image
    im = Image.open(io.BytesIO(pv.content)).convert("RGB")
    assert len(set(im.getdata())) > 1

    # (g) cleaning safety end to end
    dec = _decomposed(out)
    assert any(e.dxftype() == "LWPOLYLINE" and len(e) == 3 and e.dxf.layer.endswith(synth.LAYER_PLATE)
               for e in dec)
    assert any(e.dxf.layer.endswith(synth.LAYER_BEAM) for e in dec)
    texts = {gt._text_prim(e).text.strip() for e in dec if e.dxftype() in ("TEXT", "MTEXT", "ATTRIB")}
    assert f"{PREFIX}D" in texts and f"{PREFIX}3" in texts
