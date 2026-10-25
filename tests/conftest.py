#!/usr/bin/env python3
"""conftest.py — pytest fixtures for overlay-app tests.

Key responsibilities:
  - Monkey-patch `jobs.JOBS_DIR` to a `tmp_path` per test so we don't
    pollute the live `jobs/` directory.
  - Provide a `fx_job_with_cleaned_dxfs` fixture that creates a job,
    drops the TP-104 cleaned DXFs into it, and yields the job_id.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

# Make sure overlay-app is importable.
import sys
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def fx_isolated_jobs(monkeypatch, tmp_path):
    """Redirect jobs.JOBS_DIR to a tmp dir for the duration of one test."""
    from jobs import JOBS_DIR
    monkeypatch.setattr("jobs.JOBS_DIR", tmp_path)
    return tmp_path


ISGEC = Path(os.environ.get(
    "ISGEC_DIR",
    str(ROOT.parent / "ISGEC"),
))


@pytest.fixture
def fx_tp104_dxfs(tmp_path):
    """Stage the TP-104 fixtures into a tmp dir with the legacy
    `_clean.dxf` suffix that the existing stages' globs expect.

    The raw input files end in `clean3.dxf`, and the existing globs
    use the literal pattern `*_clean.dxf` — which won't match
    `clean3.dxf`. We mirror what `convert.py`/`clean_v*.py` does in
    production: rename so the cleaned-filename suffix is exactly
    `_clean.dxf`.
    """
    src_mech = ISGEC / "dxf" / "TP-104 MECH GA_clean3.dxf"
    src_str = ISGEC / "dxf" / "TP-104 STR GA_clean3.dxf"
    if not src_mech.exists() or not src_str.exists():
        pytest.skip(f"TP-104 fixtures missing: {src_mech} or {src_str}")
    mech_dest = tmp_path / "mech_clean.dxf"
    str_dest  = tmp_path / "str_clean.dxf"
    shutil.copyfile(src_mech, mech_dest)
    shutil.copyfile(src_str,  str_dest)
    return mech_dest, str_dest


@pytest.fixture
def fx_job_with_cleaned_dxfs(fx_isolated_jobs, fx_tp104_dxfs, tmp_path):
    """Create a job, copy the renamed TP-104 DXFs into cleaned/, write
    a convention.json. Yields the job_id.

    The cleaner normally produces the `_clean.dxf` suffix when it
    renames the output — we mirror that here so the existing glob
    `*_clean.dxf` in fit.py/spotcheck.py resolves exactly two files.
    """
    mech, struct = fx_tp104_dxfs
    from jobs import create_job_legacy, save_job, job_dir

    job = create_job_legacy(
        drawing_a_name=mech.name,
        drawing_a_size=mech.stat().st_size,
        drawing_b_name=struct.name,
        drawing_b_size=struct.stat().st_size,
        clean_version="v1",
    )
    d = job_dir(job.job_id)
    (d / "cleaned").mkdir(exist_ok=True)
    shutil.copyfile(mech,  d / "cleaned" / mech.name)
    shutil.copyfile(struct, d / "cleaned" / struct.name)

    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps({
        "prefix": "TP104-",
        "axes":   ["A", "B", "C", "D", "1", "2", "3"],
    }))

    # Mark the job as state="done" so endpoints that gate on state pass.
    job.state = "done"
    save_job(job)
    return job.job_id


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic MECH/STR fixtures (tests/synth.py)
# ─────────────────────────────────────────────────────────────────────────────

sys.path.insert(0, str(HERE))


@pytest.fixture
def fx_synth_simple(tmp_path):
    """SynthPair built from LEGACY_COMPATIBLE (rotation 0, mm, simple grid)."""
    import synth
    return synth.build_pair(tmp_path / "synth_simple", synth.LEGACY_COMPATIBLE)


@pytest.fixture
def fx_synth_hard(tmp_path):
    """SynthPair built from HARD (7.5 deg, metres, rotated inserts, MTEXT/ATTRIB)."""
    import synth
    return synth.build_pair(tmp_path / "synth_hard", synth.HARD)


@pytest.fixture
def fx_synth_job(fx_isolated_jobs, tmp_path):
    """Factory: ``fx_synth_job(opts=None, clean_version="v1") -> (job_id, SynthPair)``.

    Creates a legacy job (drawing_a = MECH, drawing_b = STR) whose
    uploads/ holds the synthetic pair. The pipeline is NOT started.
    """
    def _make(opts=None, *, clean_version: str = "v1"):
        import synth
        from jobs import create_job_legacy, job_dir
        opts = opts or synth.LEGACY_COMPATIBLE
        pair = synth.build_pair(tmp_path / f"synth_job_src_{uuid.uuid4().hex[:6]}", opts)
        job = create_job_legacy(
            drawing_a_name=pair.mech_path.name,
            drawing_a_size=pair.mech_path.stat().st_size,
            drawing_b_name=pair.str_path.name,
            drawing_b_size=pair.str_path.stat().st_size,
            clean_version=clean_version,
        )
        up = job_dir(job.job_id) / "uploads"
        up.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pair.mech_path, up / pair.mech_path.name)
        shutil.copyfile(pair.str_path, up / pair.str_path.name)
        return job.job_id, pair
    return _make
