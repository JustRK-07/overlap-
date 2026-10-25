"""Step 2 — robust file handling (core/dxfio.py, convert, cleaners)."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import ezdxf
import pytest
from ezdxf import recover

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))


def _counts(doc) -> dict:
    out = {"*msp": len(list(doc.modelspace()))}
    for b in doc.blocks:
        out[b.name] = len(list(b))
    return out


def _sha(p: Path) -> str:
    """Content hash. ezdxf stamps new GUIDs / timestamps on every save, so a
    raw byte hash can never be stable; hash the geometry-bearing content
    (all DXF tag lines minus the volatile header values) instead."""
    import re
    text = p.read_text(errors="replace")
    text = re.sub(r"\{[0-9A-Fa-f-]{36}\}", "{GUID}", text)
    text = re.sub(r"\d{4}-\d\d-\d\dT[\d:.+]+", "TS", text)
    # $TDCREATE/$TDUPDATE style julian-date values (7-digit integer part).
    text = re.sub(r"(?m)^24\d{5}\.\d+$", "JD", text)
    return hashlib.sha256(text.encode()).hexdigest()


def test_recover_and_readfile_agree_on_synth(fx_synth_simple, fx_synth_hard):
    for pair in (fx_synth_simple, fx_synth_hard):
        for p in (pair.mech_path, pair.str_path):
            plain = ezdxf.readfile(str(p))
            rec, _ = recover.readfile(str(p))
            assert _counts(plain) == _counts(rec), p.name


def test_convert_rerun_is_idempotent(fx_synth_job):
    from core import convert
    from jobs import job_dir

    job_id, pair = fx_synth_job()
    convert.run(job_id)
    d = job_dir(job_id)
    outs = sorted((d / "converted").glob("*.dxf"))
    assert [o.name for o in outs] == sorted([pair.mech_path.name, pair.str_path.name])
    assert not list((d / "cleaned").glob("*.dxf"))
    before = {o.name: _sha(o) for o in outs}
    audit1 = json.loads((d / "stages" / "audit.json").read_text())
    assert f"convert:{pair.mech_path.name}" in audit1

    convert.run(job_id)  # must not raise
    assert {o.name: _sha(o) for o in outs} == before
    assert json.loads((d / "stages" / "audit.json").read_text()).keys() == audit1.keys()


def test_convert_dwg_passes_replace_true(fx_synth_job, monkeypatch):
    """ODA is not installed: mock odafc and assert replace=True is passed."""
    from ezdxf.addons import odafc
    from core import convert
    from jobs import job_dir, load_job, save_job

    job_id, pair = fx_synth_job()
    d = job_dir(job_id)
    # Turn the MECH upload into a "DWG" (content irrelevant, odafc is mocked).
    job = load_job(job_id)
    mech_key = "drawing_a"
    meta = job.files[mech_key]
    old = meta["name"] if isinstance(meta, dict) else meta.name
    new = Path(old).stem + ".dwg"
    if isinstance(meta, dict):
        meta["name"] = new
    else:
        meta.name = new
    save_job(job)
    (d / "uploads" / new).write_bytes(b"AC1032 fake")

    calls = []

    def fake_convert(src, dst, **kw):
        calls.append(kw)
        shutil.copyfile(pair.mech_path, dst)
        return Path(dst)

    monkeypatch.setattr(odafc, "is_installed", lambda: True)
    monkeypatch.setattr(odafc, "convert", fake_convert)
    convert.run(job_id)
    convert.run(job_id)
    assert len(calls) == 2
    assert all(c.get("replace") is True and c.get("version") == "R2018"
               and c.get("audit") is True for c in calls)
    assert (d / "converted" / (Path(new).stem + ".dxf")).exists()


def test_upload_named_clean_is_cleaned(fx_synth_job, tmp_path):
    import pipelines
    from jobs import job_dir, load_job

    job_id, pair = fx_synth_job()
    d = job_dir(job_id)
    job = load_job(job_id)
    # Rename the STR upload to foo_clean.dxf.
    for key, meta in job.files.items():
        n = meta["name"] if isinstance(meta, dict) else meta.name
        if n == pair.str_path.name:
            if isinstance(meta, dict):
                meta["name"] = "foo_clean.dxf"
            else:
                meta.name = "foo_clean.dxf"
    from jobs import save_job
    save_job(job)
    (d / "uploads" / pair.str_path.name).rename(d / "uploads" / "foo_clean.dxf")

    pipelines.run_pipeline(job_id)
    assert (d / "converted" / "foo_clean.dxf").exists()
    assert (d / "cleaned" / "foo_clean_clean.dxf").exists()
    stats = json.loads((d / "stages" / "clean_stats.json").read_text())
    assert any(s.get("input") == "foo_clean.dxf" and "error" not in s for s in stats), stats


def test_clean_policy_ignores_previous_outputs(fx_synth_job):
    from core import clean_policy, convert
    from jobs import job_dir

    job_id, pair = fx_synth_job()
    convert.run(job_id)
    d = job_dir(job_id)
    (d / "stages").mkdir(exist_ok=True)
    clean_policy.run(job_id)
    p1 = (d / "stages" / "cleaning_policy.json")
    ref = p1.read_text() if p1.exists() else None

    # Stale junk output that sorts first and is not part of the manifest.
    (d / "cleaned" / "a_stale_clean.dxf").write_text("garbage not a dxf")
    (d / "cleaned" / "x_clean.dxf").write_text("garbage not a dxf")
    clean_policy.run(job_id)  # must not read the junk
    if ref is not None:
        assert p1.read_text() == ref


def test_read_input_reports_audit_fixes(tmp_path):
    from core import dxfio

    doc = ezdxf.new("R2018")
    # Entity on an undefined layer with an undefined linetype.
    doc.modelspace().add_line((0, 0), (1, 1),
                              dxfattribs={"layer": "NOT_DEFINED", "linetype": "NOLT"})
    p = tmp_path / "bad_layer.dxf"
    doc.saveas(str(p))
    d2, summary = dxfio.read_input(p)
    assert summary["fixes"] >= 1
    assert summary["errors"] == []
    assert len(list(d2.modelspace())) >= 1


def test_read_input_raises_on_unreadable(tmp_path):
    from core import dxfio

    p = tmp_path / "junk.dxf"
    p.write_text("not a dxf at all")
    with pytest.raises(dxfio.DXFReadError):
        dxfio.read_input(p)
