#!/usr/bin/env python3
"""test_roles.py — explicit MECH/STR roles (PLAN.md step 1)."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import synth  # noqa: E402
from core import roles  # noqa: E402
from core.roles import FileEntry, RoleError  # noqa: E402
from jobs import create_job_legacy, job_dir  # noqa: E402


def _entry(fid, name, role):
    stem = Path(name).stem
    return FileEntry(fid, f"uploads/{name}", role,
                     f"cleaned/{stem}.dxf", f"cleaned/{stem}_clean.dxf")


def _touch(d: Path, *names):
    (d / "cleaned").mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / "cleaned" / n).write_text("0\nEOF\n")


def _job(fx_isolated_jobs, a="a.dxf", b="b.dxf"):
    job = create_job_legacy(drawing_a_name=a, drawing_a_size=1,
                            drawing_b_name=b, drawing_b_size=1)
    return job.job_id, job_dir(job.job_id)


def test_resolve_pair_uses_manifest_not_names(fx_isolated_jobs):
    job_id, d = _job(fx_isolated_jobs)
    _touch(d, "zz_plant_clean.dxf", "aa_frame_clean.dxf")
    roles.write_manifest(job_id, [_entry("1", "aa_frame.dxf", "str"),
                                  _entry("2", "zz_plant.dxf", "mech")])
    mech, strc = roles.resolve_pair(job_id)
    assert mech.name == "zz_plant_clean.dxf"
    assert strc.name == "aa_frame_clean.dxf"


def test_resolve_pair_raises_on_ambiguous(fx_isolated_jobs):
    job_id, d = _job(fx_isolated_jobs)
    _touch(d, "m1_clean.dxf", "m2_clean.dxf", "s_clean.dxf")
    roles.write_manifest(job_id, [_entry("1", "m1.dxf", "mech"),
                                  _entry("2", "m2.dxf", "mech"),
                                  _entry("3", "s.dxf", "str")])
    with pytest.raises(RoleError):
        roles.resolve_pair(job_id)
    # N-file legacy stages opt in to "first of each" explicitly (upload order).
    mech, _ = roles.resolve_pair(job_id, first=True)
    assert mech.name == "m1_clean.dxf"
    # and a missing role is an error too
    roles.write_manifest(job_id, [_entry("1", "m1.dxf", "mech")])
    with pytest.raises(RoleError):
        roles.resolve_pair(job_id)


def test_resolve_pair_pair_dir_reads_pair_json(fx_isolated_jobs, tmp_path):
    job_id, _ = _job(fx_isolated_jobs)
    root = tmp_path / "pairroot"
    (root / "cleaned").mkdir(parents=True)
    for n in ("aa_clean.dxf", "zz_clean.dxf"):
        (root / "cleaned" / n).write_text("x")
    roles.write_pair_json(root, "zz_clean.dxf", "aa_clean.dxf", "pid")
    mech, strc = roles.resolve_pair(job_id, root / "cleaned")
    assert (mech.name, strc.name) == ("zz_clean.dxf", "aa_clean.dxf")


def test_legacy_fallback_drawing_a_is_mech(fx_isolated_jobs):
    """No manifest: drawing_a = MECH, drawing_b = STR, whatever the names sort like."""
    job_id, d = _job(fx_isolated_jobs, a="zz_plant.dxf", b="aa_frame.dxf")
    assert roles.load_manifest(job_id) is None
    _touch(d, "zz_plant_clean.dxf", "aa_frame_clean.dxf")
    mech, strc = roles.resolve_pair(job_id)
    assert (mech.name, strc.name) == ("zz_plant_clean.dxf", "aa_frame_clean.dxf")
    ents = roles.resolve_inputs(job_id)
    assert [(e.name, e.role) for e in ents] == [("zz_plant.dxf", "mech"),
                                                ("aa_frame.dxf", "str")]
    # Old job.json without "kind" keys still maps by legacy key.
    jj = json.loads((d / "job.json").read_text())
    for k in ("drawing_a", "drawing_b"):
        jj["files"][k].pop("kind")
    (d / "job.json").write_text(json.dumps(jj))
    assert [e.role for e in roles.resolve_inputs(job_id)] == ["mech", "str"]
    # Pre-cleaned fixture layout: file already named *_clean.dxf.
    job_id2, d2 = _job(fx_isolated_jobs, a="m_clean.dxf", b="s_clean.dxf")
    _touch(d2, "m_clean.dxf", "s_clean.dxf")
    assert [p.name for p in roles.resolve_pair(job_id2)] == ["m_clean.dxf", "s_clean.dxf"]


def test_clean_marks_str_by_role(fx_synth_job):
    import pipelines
    from jobs import load_job
    job_id, pair = fx_synth_job(synth.LEGACY_COMPATIBLE)
    assert pair.mech_path.name == "zz_plant.dxf" and pair.str_path.name == "aa_frame.dxf"
    pipelines.run_pipeline(job_id)
    job = load_job(job_id)
    assert job.state == "awaiting_axes", (job.state, job.error)
    d = job_dir(job_id)
    stats = {s["input"]: s for s in json.loads((d / "stages" / "clean_stats.json").read_text())}
    assert stats["aa_frame.dxf"]["is_str"] is True
    assert stats["zz_plant.dxf"]["is_str"] is False
    man = {e.name: e.role for e in roles.load_manifest(job_id)}
    assert man == {"zz_plant.dxf": "mech", "aa_frame.dxf": "str"}
    assert (d / "cleaned" / "zz_plant_clean.dxf").exists()
    assert (d / "cleaned" / "aa_frame_clean.dxf").exists()
    mech, strc = roles.resolve_pair(job_id)
    assert (mech.name, strc.name) == ("zz_plant_clean.dxf", "aa_frame_clean.dxf")
    assert job.files["drawing_a"]["cleaned_filename"] == "zz_plant_clean.dxf"


def test_upload_same_filename_rejected(fx_isolated_jobs):
    from fastapi.testclient import TestClient
    from app import app
    c = TestClient(app)
    r = c.post("/api/v1/overlay", files={
        "drawing_a": ("same.dxf", io.BytesIO(b"0\nEOF\n"), "application/dxf"),
        "drawing_b": ("same.dxf", io.BytesIO(b"0\nEOF\n"), "application/dxf"),
    })
    assert r.status_code == 400, r.text
    assert "same filename" in r.text
