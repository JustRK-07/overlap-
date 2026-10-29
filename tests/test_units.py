#!/usr/bin/env python3
"""tests/test_units.py — explicit drawing units.

A drawing's ``$INSUNITS`` header is a claim, not a fact.  The TP-104 MECH
drawing claims inches and is drawn in millimetres, and a pipeline that
believes the header either refuses the fit or — worse — scales the
geometry by 25.4.

So the units are read twice and compared:

* the **header** says what the drawing claims;
* the **grid** says what the drawings really are, because two drawings of
  one building share a physical grid spacing, so the ratio of the two
  median spacings is the scale between them whatever the headers claim.

These tests cover the four cases that matter:

* both drawings in millimetres — the readings agree, nothing to ask;
* an inch header on millimetre geometry — the readings disagree, the job
  pauses and says what the grid indicates;
* the same pair with the units chosen by hand — the conflict is resolved
  and the fit runs at the right scale;
* an offset of 1, 2, 5 and 10 mm — converted to the drawing's units and
  back, exactly.

And two guarantees: the user's file is never modified, and the report
records what was claimed, what was used and why.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import ezdxf
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import synth                                   # noqa: E402
from core import units as units_mod            # noqa: E402

PREFIX = "TP104-"
AXES = ["A", "B", "C", "D", "1", "2", "3"]

#: The units a user may choose, and what one of them is worth in mm.
UNIT_FACTORS = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _set_insunits(path: Path, code: int) -> None:
    """Rewrite a drawing's ``$INSUNITS`` header — the geometry is untouched."""
    doc = ezdxf.readfile(str(path))
    doc.header["$INSUNITS"] = code
    doc.saveas(str(path))


def _pair_with_headers(tmp_path, *, mech=4, str_=4, opts=None):
    """A synthetic pair whose headers say what we ask for."""
    pair = synth.build_pair(tmp_path / f"src_{mech}_{str_}", opts or synth.LEGACY_COMPATIBLE)
    _set_insunits(pair.mech_path, mech)
    _set_insunits(pair.str_path, str_)
    return pair


def _job_from_pair(tmp_path, pair, *, run_pipeline=True):
    """A job holding ``pair``, with stages 1–6 run."""
    from jobs import create_job_legacy, job_dir, load_job

    job = create_job_legacy(
        drawing_a_name=pair.mech_path.name,
        drawing_a_size=pair.mech_path.stat().st_size,
        drawing_b_name=pair.str_path.name,
        drawing_b_size=pair.str_path.stat().st_size,
        clean_version="v1",
    )
    up = job_dir(job.job_id) / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pair.mech_path, up / pair.mech_path.name)
    shutil.copyfile(pair.str_path, up / pair.str_path.name)
    if run_pipeline:
        import pipelines
        pipelines.run_pipeline(job.job_id)
    return job.job_id, job_dir(job.job_id)


def _decision(job_id):
    """The units decision for a job, from its confirmed grid."""
    from core import fit as fit_mod
    return fit_mod.units_decision(job_id)


# ─────────────────────────────────────────────────────────────────────────────
# 1 · both drawings in millimetres
# ─────────────────────────────────────────────────────────────────────────────

def test_both_drawings_in_mm_agree(fx_isolated_jobs, tmp_path):
    """Two mm drawings: the grid and the headers say the same thing."""
    pair = _pair_with_headers(tmp_path, mech=4, str_=4)
    job_id, d = _job_from_pair(tmp_path, pair)

    # The convention has to be confirmed before the units can be checked.
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": PREFIX, "axes": AXES}))

    dec = _decision(job_id)
    assert dec.conflict is False
    assert dec.grid_scale == pytest.approx(1.0)
    assert dec.header_scale == pytest.approx(1.0)
    assert dec.mech.name == "mm" and dec.str.name == "mm"
    assert dec.mech.source == "header" and dec.str.source == "header"

    # ...and the fit runs on it.
    from core import fit as fit_mod
    fit_mod.run(job_id)
    t = json.loads((d / "transform.json").read_text())
    assert t["scale"] == pytest.approx(1.0, rel=1e-9)
    assert t["quality"]["status"] == "ok"
    assert t["units"]["mech"] == 4 and t["units"]["str"] == 4


# ─────────────────────────────────────────────────────────────────────────────
# 2 · an inch header on millimetre geometry
# ─────────────────────────────────────────────────────────────────────────────

def test_an_inch_header_on_mm_geometry_conflicts(fx_isolated_jobs, tmp_path):
    """The MECH header says inch; the grid says both drawings are in mm."""
    pair = _pair_with_headers(tmp_path, mech=1, str_=4)   # 1 == inch
    job_id, d = _job_from_pair(tmp_path, pair)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": PREFIX, "axes": AXES}))

    dec = _decision(job_id)
    assert dec.conflict is True
    # the grid says 1.0, the headers say 1 mm → 1 inch
    assert dec.grid_scale == pytest.approx(1.0)
    assert dec.header_scale == pytest.approx(1.0 / 25.4, rel=1e-9)

    # ...and the suggestion is the one the geometry supports.
    assert dec.suggestion is not None
    assert dec.suggestion.mech == "mm" and dec.suggestion.str == "mm"
    assert dec.suggestion.message() == \
        "grid spacing indicates both drawings are in mm"
    assert "grid spacing indicates both drawings are in mm" in dec.message

    # The inch reading would put the grid 139 m apart — not a building.
    assert dec.suggestion.spacing_mm == pytest.approx(5500.0, rel=0.02)
    assert dec.suggestion.plausible is True


def test_the_overriding_units_are_recorded_and_the_fit_runs(fx_isolated_jobs, tmp_path):
    """Choosing the units by hand resolves the conflict and the fit is exact."""
    from jobs import load_job, save_job

    pair = _pair_with_headers(tmp_path, mech=1, str_=4)   # 1 == inch
    job_id, d = _job_from_pair(tmp_path, pair)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": PREFIX, "axes": AXES}))

    # The user's answer, recorded in job.json.
    job = load_job(job_id)
    job.unit_overrides = {"drawing_a": "mm", "drawing_b": "mm"}
    save_job(job)

    dec = _decision(job_id)
    assert dec.conflict is False
    assert dec.mech.name == "mm" and dec.str.name == "mm"
    assert dec.mech.source == "override" and dec.str.source == "override"
    # the header is still recorded — only the *use* of it changed
    assert dec.mech.header_code == 1 and dec.mech.header_name == "in"

    from core import fit as fit_mod
    fit_mod.run(job_id)
    t = json.loads((d / "transform.json").read_text())
    assert t["scale"] == pytest.approx(1.0, rel=1e-9)
    assert t["quality"]["status"] == "ok"
    assert t["units"]["mech"] == 4            # the chosen code
    assert t["units"]["mech_header"] == 1     # the header it replaced
    assert t["units"]["mech_units"] == "mm"
    assert t["units"]["mech_source"] == "override"
    assert "by hand" in t["units"]["mech_reason"]


def test_the_mismatch_engine_uses_the_chosen_units(fx_isolated_jobs, tmp_path):
    """``mm_per_unit_of`` honours the override — 1.0, not 25.4."""
    from core.mismatch.features import mm_per_unit_of

    pair = _pair_with_headers(tmp_path, mech=1, str_=4)   # 1 == inch
    doc = ezdxf.readfile(str(pair.mech_path))

    assert mm_per_unit_of(doc) == (25.4, False)          # the header alone
    assert mm_per_unit_of(doc, "mm") == (1.0, False)      # the override
    assert mm_per_unit_of(doc, None) == (25.4, False)     # no override


# ─────────────────────────────────────────────────────────────────────────────
# 3 · contradictory headers without an override
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def fx_client(fx_isolated_jobs):
    from fastapi.testclient import TestClient
    from app import app as fastapi_app
    return TestClient(fastapi_app)


@pytest.fixture
def fx_no_worker(monkeypatch):
    """Stop the *confirm* worker from touching the job.

    Stages 1–6 still run for real — the cleaned drawings are what the
    units are checked against — but the pipeline does not resume when
    the axes are confirmed.
    """
    import pipelines
    monkeypatch.setattr(pipelines, "resume_after_axes", lambda *a, **k: None)
    return True


def test_contradictory_headers_pause_without_an_override(fx_client, fx_isolated_jobs,
                                                         fx_no_worker, tmp_path):
    """Without an answer the job stays at awaiting_axes and says why."""
    from jobs import load_job

    pair = _pair_with_headers(tmp_path, mech=1, str_=4)   # 1 == inch
    job_id, d = _job_from_pair(tmp_path, pair)

    # The axes step: the conflict is reported, and the job does not move on.
    r = fx_client.post(f"/api/v1/overlay/{job_id}/axes",
                       json={"prefix": PREFIX, "axes": AXES})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["units"]["conflict"] is True
    assert "grid spacing indicates both drawings are in mm" in body["units"]["message"]
    assert body["units"]["suggestion"]["mech"] == "mm"
    assert body["units"]["suggestion"]["str"] == "mm"
    assert body["units"]["mech"]["header_units"] == "in"

    # Confirming without an answer is refused, and the job stays put.
    rc = fx_client.post(f"/api/v1/overlay/{job_id}/axes/confirm")
    assert rc.status_code == 409, rc.text
    assert "grid spacing indicates both drawings are in mm" in rc.json()["detail"]
    assert load_job(job_id).state == "awaiting_axes"

    # Confirming *with* the answer goes through.
    rc = fx_client.post(f"/api/v1/overlay/{job_id}/axes/confirm",
                        json={"units": {"mech": "mm", "str": "mm"}})
    assert rc.status_code == 200, rc.text
    assert rc.json()["status"] == "resuming"

    from jobs import load_job as _load
    assert _load(job_id).unit_overrides == {"drawing_a": "mm", "drawing_b": "mm"}


def test_a_bad_unit_is_a_400(fx_client, fx_isolated_jobs, fx_no_worker, tmp_path):
    """An unknown unit names the field and the accepted values."""
    pair = _pair_with_headers(tmp_path, mech=4, str_=4)
    job_id, d = _job_from_pair(tmp_path, pair)

    r = fx_client.post(f"/api/v1/overlay/{job_id}/axes",
                       json={"prefix": PREFIX, "axes": AXES,
                             "units": {"mech": "furlong", "str": "mm"}})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "units.mech" in detail
    assert "mm" in detail and "in" in detail


# ─────────────────────────────────────────────────────────────────────────────
# 4 · offsets of 1, 2, 5 and 10 mm convert exactly
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("offset_mm", [1, 2, 5, 10])
@pytest.mark.parametrize("unit", list(UNIT_FACTORS))
def test_a_millimetre_offset_converts_exactly(offset_mm, unit):
    """An offset of N mm, expressed in the drawing's units, converts back to N mm.

    The mismatch engine compares everything in millimetres, so a length read
    off a drawing in centimetres has to survive the round trip exactly —
    a 10 mm offset may not come back as 9.999999 mm.
    """
    from core.mismatch.checks import Env
    from core.mismatch.features import FeatureSet
    from core.mismatch.policy import DEFAULT_POLICY

    factor = units_mod.mm_per_unit_of(unit)
    assert factor == pytest.approx(UNIT_FACTORS[unit])

    env = Env(policy=DEFAULT_POLICY, mm=factor,
              mech=FeatureSet(source="MECH"), str=FeatureSet(source="STR"))

    # The offset as it is drawn, and the millimetres it stands for.
    drawn = env.u(offset_mm)
    assert env.to_mm(drawn) == pytest.approx(offset_mm, abs=1e-12)

    # The exact value a drawing in that unit would use.
    if unit == "mm":
        assert drawn == offset_mm
    elif unit == "cm":
        assert drawn == offset_mm / 10
    elif unit == "m":
        assert drawn == offset_mm / 1000
    elif unit == "in":
        assert drawn == pytest.approx(offset_mm / 25.4, abs=1e-15)
    elif unit == "ft":
        assert drawn == pytest.approx(offset_mm / 304.8, abs=1e-15)

    # A search radius in millimetres comes back in millimetres.
    assert env.search_mm(offset_mm) == pytest.approx(offset_mm, abs=1e-12)


# ─────────────────────────────────────────────────────────────────────────────
# the guarantees
# ─────────────────────────────────────────────────────────────────────────────

def test_the_users_file_is_never_modified(fx_isolated_jobs, tmp_path):
    """The override lives beside the job; the drawing keeps its own header."""
    from jobs import load_job, save_job
    from core import fit as fit_mod

    pair = _pair_with_headers(tmp_path, mech=1, str_=4)   # 1 == inch
    before = pair.mech_path.read_bytes()

    job_id, d = _job_from_pair(tmp_path, pair)
    job = load_job(job_id)
    job.unit_overrides = {"drawing_a": "mm", "drawing_b": "mm"}
    save_job(job)

    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": PREFIX, "axes": AXES}))
    fit_mod.run(job_id)

    # The uploaded file is byte-for-byte what it was.
    assert pair.mech_path.read_bytes() == before
    # ...and so is the copy the pipeline works from.
    uploaded = d / "uploads" / pair.mech_path.name
    assert uploaded.read_bytes() == before
    # The header still says inch — nobody rewrote the drawing.
    assert ezdxf.readfile(str(uploaded)).header["$INSUNITS"] == 1


def test_the_report_records_the_units(fx_isolated_jobs, tmp_path):
    """report.json says what the header claimed, what was used, and why."""
    from jobs import load_job, save_job
    from core import fit as fit_mod
    from core import package as package_mod

    pair = _pair_with_headers(tmp_path, mech=1, str_=4)   # 1 == inch
    job_id, d = _job_from_pair(tmp_path, pair)
    job = load_job(job_id)
    # Only the MECH drawing's units are wrong — the STR header is right
    # and is left alone.
    job.unit_overrides = {"drawing_a": "mm"}
    save_job(job)

    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": PREFIX, "axes": AXES}))
    fit_mod.run(job_id)

    # The grid spacing the axes step measured is on disk …
    from core import fit as fit_mod
    dec = fit_mod.units_decision(job_id)
    assert dec.grid_scale == pytest.approx(1.0)

    report = package_mod._units_section(job_id, json.loads(
        (d / "transform.json").read_text()), d)
    assert report["mech"]["header_insunits"] == 1
    assert report["mech"]["header_units"] == "in"
    assert report["mech"]["units"] == "mm"
    assert report["mech"]["source"] == "override"
    assert report["mech"]["reason"]
    assert report["str"]["header_units"] == "mm"
    assert report["str"]["units"] == "mm"
    assert report["str"]["source"] == "header"
    assert report["grid_scale"] == pytest.approx(1.0)
    assert report["expected_scale"] == pytest.approx(1.0)
    assert report["mm_per_unit"] == pytest.approx(1.0)
    assert "by hand" in report["reason"]

    # The overlay's own report says which units each drawing was in.
    from core import overlay as overlay_mod
    overlay_mod.run(job_id)
    overlay_report = json.loads(
        (d / "stages" / overlay_mod.REPORT_NAME).read_text())
    assert overlay_report["units"]["mech"]["units"] == "mm"
    assert overlay_report["units"]["mech"]["header_units"] == "in"
    assert overlay_report["units"]["str"]["units"] == "mm"


# ─────────────────────────────────────────────────────────────────────────────
# the exported drawing declares the units it is actually in
# ─────────────────────────────────────────────────────────────────────────────

def test_the_export_declares_the_resolved_units_not_the_header(fx_isolated_jobs, tmp_path):
    """A MECH whose header lies must not hand over a drawing that lies too.

    The TP-104 MECH says ``$INSUNITS = 1`` (inch) and is drawn in
    millimetres.  The pipeline works that out from the grid and the user
    confirms it, but the exported drawing used to carry the MECH's raw
    header — so a CAD program would have scaled the whole drawing by 25.4.
    """
    import ezdxf

    from core import fit as fit_mod
    from core import overlay as overlay_mod
    from jobs import load_job, save_job

    pair = synth.build_pair(tmp_path / "src", synth.LEGACY_COMPATIBLE)
    # the MECH header lies: it claims inches
    doc = ezdxf.readfile(str(pair.mech_path))
    doc.header["$INSUNITS"] = 1
    doc.header["$MEASUREMENT"] = 0
    doc.saveas(str(pair.mech_path))

    job_id, d = _job_from_pair(tmp_path, pair)
    # the user answers the units the way they would for the real drawing
    job = load_job(job_id)
    job.unit_overrides = {"drawing_a": "mm", "drawing_b": "mm"}
    save_job(job)

    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": "TP104-", "axes": ["A", "B", "C", "D", "1", "2", "3"]}))
    fit_mod.run(job_id)
    overlay_mod.run(job_id)

    out = ezdxf.readfile(str(d / "out" / "superimposed.dxf"))
    assert out.header["$INSUNITS"] == 4, out.header["$INSUNITS"]
    assert out.header["$MEASUREMENT"] == 1, out.header["$MEASUREMENT"]
    # …and the extents bound the geometry that was actually exported
    from ezdxf import bbox
    ext = bbox.extents(out.modelspace(), fast=True)
    assert tuple(out.header["$EXTMIN"][:2]) == pytest.approx(
        (ext.extmin.x, ext.extmin.y), abs=1e-6)
    assert tuple(out.header["$EXTMAX"][:2]) == pytest.approx(
        (ext.extmax.x, ext.extmax.y), abs=1e-6)


def test_the_export_keeps_the_geometry_it_was_drawn_with(fx_isolated_jobs, tmp_path):
    """Stamping the units must not move a single entity."""
    import ezdxf

    from core import fit as fit_mod
    from core import overlay as overlay_mod
    from jobs import load_job, save_job

    pair = synth.build_pair(tmp_path / "src", synth.LEGACY_COMPATIBLE)
    job_id, d = _job_from_pair(tmp_path, pair)
    job = load_job(job_id)
    job.unit_overrides = {"drawing_a": "mm", "drawing_b": "mm"}
    save_job(job)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": "TP104-", "axes": ["A", "B", "C", "D", "1", "2", "3"]}))
    fit_mod.run(job_id)
    overlay_mod.run(job_id)

    out = ezdxf.readfile(str(d / "out" / "superimposed.dxf"))
    mech = ezdxf.readfile(str(d / "cleaned" / (pair.mech_path.stem + "_clean.dxf")))
    # every MECH entity is still where it was drawn
    src = {(round(e.dxf.start.x, 6), round(e.dxf.start.y, 6))
            for e in mech.modelspace() if e.dxftype() == "LINE"}
    got = {(round(e.dxf.start.x, 6), round(e.dxf.start.y, 6))
           for e in out.modelspace()
           if e.dxftype() == "LINE" and str(e.dxf.layer).startswith("MECH|")}
    assert src <= got, len(src - got)
