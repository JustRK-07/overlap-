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
        strategy="A",
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
