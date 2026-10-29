#!/usr/bin/env python3
"""tests/test_e2e_synthetic.py — PLAN.md step 10.

The end-to-end flow through the **API**, against the synthetic ground
truth.  PLAN.md step 10 names three cases:

``test_e2e_synthetic_legacy_flow``
    the 2-file upload → poll → axes → confirm → download → verify, over
    two presets: HARD (metres, 7.5°) and HARD in millimetres at −3°
``test_e2e_synthetic_n_file_reversed_roles``
    the N-file upload with the roles **reversed** — the case that would
    catch a role inversion at the API boundary
``test_e2e_outlier_axis_reported``
    a shifted grid axis: the fit must reject it, report it as an outlier
    and mark it on ``QA|OUTLIER``

Every assertion is against the generator's analytic truth, so a
regression in any stage is caught rather than eyeballed.
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import geomtruth as gt          # noqa: E402
import synth                    # noqa: E402

PREFIX = "TP104-"
SUFFIXES = ["A", "B", "C", "D", "1", "2", "3"]

pytestmark = pytest.mark.slow


# ── helpers ────────────────────────────────────────────────────────────────

def _poll(client, job_id, want, timeout=90.0):
    t0 = time.time()
    st = {}
    while time.time() - t0 < timeout:
        st = client.get(f"/api/v1/overlay/{job_id}/status").json()
        if st["state"] == want:
            return st
        assert st["state"] != "failed" and not st.get("error"), st
        time.sleep(0.1)
    raise AssertionError(f"timeout waiting for {want!r}; last={st}")


def _only_layers(path_or_text, prefix, *, text=False):
    doc = ezdxf.read(io.StringIO(path_or_text)) if text \
        else ezdxf.readfile(str(path_or_text))
    msp = doc.modelspace()
    for e in list(msp):
        if not e.dxf.layer.startswith(prefix):
            msp.delete_entity(e)
    return doc


def _extent(pts) -> float:
    a = np.asarray(pts, dtype=float)
    return float(np.hypot(*np.ptp(a, axis=0)))


def _run_legacy_flow(client, pair, **data):
    """Upload → poll → axes → confirm → poll.  Returns the job id."""
    with open(pair.mech_path, "rb") as fa, open(pair.str_path, "rb") as fb:
        r = client.post(
            "/api/v1/overlay",
            files={"drawing_a": (pair.mech_path.name, fa, "application/dxf"),
                   "drawing_b": (pair.str_path.name, fb, "application/dxf")},
            data=data)
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]

    _poll(client, jid, "awaiting_axes")
    axes = client.get(f"/api/v1/overlay/{jid}/axes").json()
    assert PREFIX in json.dumps(axes), axes

    r = client.post(f"/api/v1/overlay/{jid}/axes",
                    json={"prefix": PREFIX, "axes": SUFFIXES})
    assert r.json()["all_matched"] is True, r.json()

    assert client.post(f"/api/v1/overlay/{jid}/axes/confirm").status_code == 200
    _poll(client, jid, "done")
    return jid


def _verify_the_output(client, jid, pair):
    """The plan's assertions (a) … (g) against the analytic truth."""
    from core import grid
    from jobs import job_dir

    dl = client.get(f"/api/v1/overlay/{jid}/download")
    assert dl.status_code == 200
    out = ezdxf.read(io.StringIO(dl.text))

    # (a) the grid of the STR content coincides with the grid of the MECH
    gm = grid.intersections(grid.detect_grid(
        _only_layers(dl.text, "MECH|", text=True), PREFIX, SUFFIXES))
    gs = grid.intersections(grid.detect_grid(
        _only_layers(dl.text, "STR|", text=True), PREFIX, SUFFIXES))
    diag = _extent(list(pair.intersections_mech.values()))
    shared = set(gm) & set(gs)
    assert len(shared) == 12, f"only {len(shared)} shared intersections"
    for k in shared:
        assert np.hypot(gm[k][0] - gs[k][0], gm[k][1] - gs[k][1]) <= 1e-6 * diag

    # (b) the STR content is the cleaned STR mapped by the truth
    # (c) the MECH content is the cleaned MECH, unchanged
    cdir = job_dir(jid) / "cleaned"
    str_clean = ezdxf.readfile(str(cdir / "aa_frame_clean.dxf"))
    mech_clean = ezdxf.readfile(str(cdir / "zz_plant_clean.dxf"))
    ext = float(np.abs(np.vstack([p.pts for p in gt.world_primitives(out)])).max())
    gt.assert_prims_match(
        gt.world_primitives(out, layer_pred=lambda l: l.startswith("STR|"),
                            flat=gt.FLAT_MM),
        gt.map_prims(gt.world_primitives(str_clean), pair.truth),
        gt.tolerance(ext))
    gt.assert_prims_match(
        gt.world_primitives(out, layer_pred=lambda l: l.startswith("MECH|"),
                            flat=gt.FLAT_MM),
        gt.world_primitives(mech_clean, flat=gt.FLAT_MM),
        gt.tolerance(ext))

    # (d) the transform
    t = json.loads((job_dir(jid) / "transform.json").read_text())
    assert abs(t["rotation_deg"] - pair.truth.rotation_deg) < 1e-9
    assert abs(t["scale"] - pair.truth.scale) / pair.truth.scale < 1e-9
    assert t["quality"]["status"] == "ok"
    assert t["outliers"] == []

    # (e) the report
    rep = client.get(f"/api/v1/overlay/{jid}/report.json").json()
    assert rep["overlay"]["failures"] == []
    assert rep["counts"]["str_out"] == rep["counts"]["str_in"]
    assert rep["dangling_inserts_dropped"] == 0

    # (f) the preview
    pv = client.get(f"/api/v1/overlay/{jid}/preview.png")
    assert pv.status_code == 200
    from PIL import Image
    im = Image.open(io.BytesIO(pv.content)).convert("RGB")
    assert len(set(im.getdata())) > 1

    # (g) cleaning safety end to end
    from ezdxf import disassemble
    dec = list(disassemble.recursive_decompose(out.modelspace()))
    assert any(e.dxftype() == "LWPOLYLINE" and len(e) == 3
               and e.dxf.layer.endswith(synth.LAYER_PLATE) for e in dec)
    assert any(e.dxf.layer.endswith(synth.LAYER_BEAM) for e in dec)
    texts = {gt._text_prim(e).text.strip() for e in dec
             if e.dxftype() in ("TEXT", "MTEXT", "ATTRIB")}
    assert f"{PREFIX}D" in texts and f"{PREFIX}3" in texts


# ── the three cases ────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", ["hard", "mm_minus_3"])
def test_e2e_synthetic_legacy_flow(fx_isolated_jobs, tmp_path, case):
    """The legacy 2-file flow, over two presets.

    ``hard`` is metres at 7.5°; ``mm_minus_3`` is millimetres at −3°.
    The second one exists because a fit that only works for one sign of
    the rotation is not a fit.
    """
    from fastapi.testclient import TestClient

    from app import app

    if case == "hard":
        opts = synth.HARD
    else:
        opts = synth.with_opts(synth.HARD, str_units="mm",
                                str_rotation_deg=-3.0)
    pair = synth.build_pair(tmp_path / f"e2e_{case}", opts)

    client = TestClient(app)
    jid = _run_legacy_flow(client, pair, color_mode="contrast")
    _verify_the_output(client, jid, pair)


def test_e2e_synthetic_n_file_reversed_roles(fx_isolated_jobs, tmp_path):
    """The N-file upload with the roles **reversed**.

    The files are uploaded STR-first with the roles ``str,mech``.  A
    pipeline that guessed the roles from the order would superimpose the
    wrong drawing — this is the case that catches it.
    """
    from fastapi.testclient import TestClient

    from app import app
    from core import grid
    from jobs import job_dir

    pair = synth.build_pair(tmp_path / "e2e_reversed", synth.HARD)
    client = TestClient(app)

    with open(pair.str_path, "rb") as fs, open(pair.mech_path, "rb") as fm:
        r = client.post(
            "/api/v1/overlay/upload",
            files=[("files", (pair.str_path.name, fs, "application/dxf")),
                   ("files", (pair.mech_path.name, fm, "application/dxf"))],
            data={"roles": "str,mech", "prefix": PREFIX,
                  "axes": " ".join(SUFFIXES)})
    assert r.status_code == 200, r.text
    body = r.json()
    jid = body["job_id"]
    assert body["pair_count"] == 1, body
    pair_id = body["pairs"][0]["pair_id"]

    _poll(client, jid, "done", timeout=180.0)

    dl = client.get(f"/api/v1/overlay/{jid}/download")
    assert dl.status_code == 200
    out = ezdxf.read(io.StringIO(dl.text))

    # the STR content still lands on the MECH grid
    gm = grid.intersections(grid.detect_grid(
        _only_layers(dl.text, "MECH|", text=True), PREFIX, SUFFIXES))
    gs = grid.intersections(grid.detect_grid(
        _only_layers(dl.text, "STR|", text=True), PREFIX, SUFFIXES))
    diag = _extent(list(pair.intersections_mech.values()))
    shared = set(gm) & set(gs)
    assert len(shared) == 12, f"only {len(shared)} shared intersections"
    for k in shared:
        assert np.hypot(gm[k][0] - gs[k][0], gm[k][1] - gs[k][1]) <= 1e-6 * diag

    # and the transform is the truth, not its mirror.  In the N-file layout
    # it lives under the pair's own root.
    t = json.loads(
        (job_dir(jid) / "out" / "pairs" / pair_id / "transform.json").read_text())
    assert abs(t["rotation_deg"] - pair.truth.rotation_deg) < 1e-9
    assert abs(t["scale"] - pair.truth.scale) / pair.truth.scale < 1e-9


def test_e2e_outlier_axis_reported(fx_isolated_jobs, tmp_path):
    """A shifted grid axis is rejected, reported and marked.

    The STR's "D" axis is shifted 300 mm.  The fit must drop it as an
    outlier, still succeed on the rest, and mark the rejected
    intersections on ``QA|OUTLIER``.
    """
    from fastapi.testclient import TestClient

    from app import app
    from jobs import job_dir

    opts = synth.with_opts(synth.LEGACY_COMPATIBLE, shift_str_axis={"D": 300})
    pair = synth.build_pair(tmp_path / "e2e_outlier", opts)

    client = TestClient(app)
    jid = _run_legacy_flow(client, pair)

    t = json.loads((job_dir(jid) / "transform.json").read_text())
    assert t["quality"]["status"] == "ok"
    assert t["outliers"], "the shifted axis must be reported as an outlier"

    out = ezdxf.readfile(str(job_dir(jid) / "out" / "superimposed.dxf"))
    assert out.layers.has_entry("QA|OUTLIER")
    lines = [e for e in out.modelspace() if e.dxf.layer == "QA|OUTLIER"]
    # each marker is an X of two crossing LINEs — group them by centre
    centres = set()
    for e in lines:
        a = (round(e.dxf.start.x, 6), round(e.dxf.start.y, 6))
        b = (round(e.dxf.end.x, 6), round(e.dxf.end.y, 6))
        centres.add((round((a[0] + b[0]) / 2.0, 3),
                     round((a[1] + b[1]) / 2.0, 3)))
    assert len(centres) == 3, (
        f"the three rejected D intersections must be marked, got {len(centres)} "
        f"({len(lines)} lines)")
