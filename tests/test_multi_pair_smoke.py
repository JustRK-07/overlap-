#!/usr/bin/env python3
"""test_multi_pair_smoke.py — Slice 2 multi-pair end-to-end smoke.

Exercises `run_pairs_for_job` against the real TP-104 fixtures (we have
only 2 in `ISGEC/dxf/`, so we copy one of them twice as the 2nd STR).
Asserts:
  - 2 pairs run, both with status="done" in job.pairs
  - metadata.json exists for each pair root
  - GET /pairs returns 2 entries
  - GET /analyze returns {pairs: [...]} with 2 entries
  - Re-running a pair produces byte-identical transform (idempotent)

Marked `@pytest.mark.slow` because it loads two large DXFs through the
full pipeline twice. Skip with `-m "not slow"`.
"""
from __future__ import annotations

import json
import shutil
import sys
from io import BytesIO
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import app as fastapi_app  # noqa: E402
from core.pair_runner import encode_pair_id, pair_root, run_pairs_for_job  # noqa: E402
from jobs import create_job, job_dir, load_job, save_job  # noqa: E402


@pytest.fixture
def fx_client(fx_isolated_jobs):
    return TestClient(fastapi_app)


@pytest.fixture
def fx_three_dxfs(fx_tp104_dxfs, tmp_path):
    """Stage 3 DXFs (1 MECH + 2 STR — second is a copy of the first)."""
    mech, struct = fx_tp104_dxfs
    str2 = tmp_path / "str2_clean.dxf"
    shutil.copyfile(struct, str2)
    return mech, struct, str2


@pytest.mark.slow
def test_run_pairs_for_job_runs_both_pairs_to_done(fx_isolated_jobs, fx_three_dxfs, tmp_path):
    """run_pairs_for_job processes all MECH×STR pairs to status=done."""
    mech, s1, s2 = fx_three_dxfs

    # Build a job with 3 files. (We bypass create_job's auto-pair so we
    # can attach cleaned_filename explicitly.)
    job = create_job(
        files=[
            {"name": mech.name, "size": mech.stat().st_size, "kind": "mech"},
            {"name": s1.name,   "size": s1.stat().st_size,   "kind": "str"},
            {"name": s2.name,   "size": s2.stat().st_size,   "kind": "str"},
        ],
    )

    # Manually populate FileMeta.cleaned_filename (the cleaner would
    # normally do this) + copy files into the job's cleaned/ dir.
    d = job_dir(job.job_id)
    (d / "cleaned").mkdir(exist_ok=True)
    shutil.copyfile(mech, d / "cleaned" / mech.name)
    shutil.copyfile(s1,   d / "cleaned" / s1.name)
    shutil.copyfile(s2,   d / "cleaned" / s2.name)
    for fid, meta in job.files.items():
        if isinstance(meta, dict):
            meta["cleaned_filename"] = meta["name"]
        else:
            meta.cleaned_filename = meta.name
    save_job(job)

    # Write a convention.json so fit.py can read it.
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps({
        "prefix": "TP104-",
        "axes": ["A", "B", "C", "D", "1", "2", "3"],
    }))

    # Run all pairs.
    results = run_pairs_for_job(
        job.job_id,
        pairs=list(job.pairs),
        prefix="TP104-",
        axes=["A", "B", "C", "D", "1", "2", "3"],
    )

    assert len(results) == 2
    for r in results:
        assert r["status"] == "done", r
        assert "metrics" in r

    # Reload the job and check Pair statuses.
    loaded = load_job(job.job_id)
    assert loaded is not None
    for pair in loaded.pairs:
        assert pair.status == "done", pair
        assert pair.metrics
        assert pair.artifacts
        assert pair.completed_at

    # Each pair root should have metadata.json + transform.json.
    for pair in loaded.pairs:
        root = pair_root(job.job_id, pair.pair_id)
        assert (root / "metadata.json").exists()
        assert (root / "transform.json").exists()


@pytest.mark.slow
def test_pairs_endpoint_returns_both_pairs(fx_client, fx_isolated_jobs, fx_three_dxfs):
    """After run_pairs_for_job, GET /pairs lists both pairs."""
    mech, s1, s2 = fx_three_dxfs
    job = create_job(
        files=[
            {"name": mech.name, "size": mech.stat().st_size, "kind": "mech"},
            {"name": s1.name,   "size": s1.stat().st_size,   "kind": "str"},
            {"name": s2.name,   "size": s2.stat().st_size,   "kind": "str"},
        ],
    )
    d = job_dir(job.job_id)
    (d / "cleaned").mkdir(exist_ok=True)
    shutil.copyfile(mech, d / "cleaned" / mech.name)
    shutil.copyfile(s1,   d / "cleaned" / s1.name)
    shutil.copyfile(s2,   d / "cleaned" / s2.name)
    for fid, meta in job.files.items():
        if isinstance(meta, dict):
            meta["cleaned_filename"] = meta["name"]
        else:
            meta.cleaned_filename = meta.name
    save_job(job)

    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps({
        "prefix": "TP104-", "axes": ["A", "B", "C", "D", "1", "2", "3"],
    }))

    run_pairs_for_job(
        job.job_id, pairs=list(job.pairs),
        prefix="TP104-", axes=["A", "B", "C", "D", "1", "2", "3"],
    )

    r = fx_client.get(f"/api/v1/overlay/{job.job_id}/pairs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["pairs"]) == 2
    for p in body["pairs"]:
        assert p["status"] == "done"
        assert "metrics" in p


@pytest.mark.slow
def test_analyze_endpoint_returns_pairs_wrapper(fx_client, fx_isolated_jobs, fx_three_dxfs):
    """After run_pairs_for_job, GET /analyze returns {pairs: [...]} with 2 entries."""
    mech, s1, s2 = fx_three_dxfs
    job = create_job(
        files=[
            {"name": mech.name, "size": mech.stat().st_size, "kind": "mech"},
            {"name": s1.name,   "size": s1.stat().st_size,   "kind": "str"},
            {"name": s2.name,   "size": s2.stat().st_size,   "kind": "str"},
        ],
    )
    d = job_dir(job.job_id)
    (d / "cleaned").mkdir(exist_ok=True)
    shutil.copyfile(mech, d / "cleaned" / mech.name)
    shutil.copyfile(s1,   d / "cleaned" / s1.name)
    shutil.copyfile(s2,   d / "cleaned" / s2.name)
    for fid, meta in job.files.items():
        if isinstance(meta, dict):
            meta["cleaned_filename"] = meta["name"]
        else:
            meta.cleaned_filename = meta.name
    save_job(job)

    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps({
        "prefix": "TP104-", "axes": ["A", "B", "C", "D", "1", "2", "3"],
    }))

    run_pairs_for_job(
        job.job_id, pairs=list(job.pairs),
        prefix="TP104-", axes=["A", "B", "C", "D", "1", "2", "3"],
    )

    r = fx_client.get(f"/api/v1/overlay/{job.job_id}/analyze")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "pairs" in body
    assert len(body["pairs"]) == 2
    for p in body["pairs"]:
        assert 0.0 <= p["rms_residual"]
        assert 0.0 <= p["max_residual"]
        assert 0 <= len(p["intersections"]) <= 16
        for ix in p["intersections"]:
            assert 0.0 <= ix["normalized_depth"] <= 1.0


@pytest.mark.slow
def test_run_pair_idempotent_when_re_run(fx_isolated_jobs, fx_three_dxfs):
    """Re-running run_pairs_for_job produces identical transform.json bytes."""
    mech, s1, s2 = fx_three_dxfs
    job = create_job(
        files=[
            {"name": mech.name, "size": mech.stat().st_size, "kind": "mech"},
            {"name": s1.name,   "size": s1.stat().st_size,   "kind": "str"},
            {"name": s2.name,   "size": s2.stat().st_size,   "kind": "str"},
        ],
    )
    d = job_dir(job.job_id)
    (d / "cleaned").mkdir(exist_ok=True)
    shutil.copyfile(mech, d / "cleaned" / mech.name)
    shutil.copyfile(s1,   d / "cleaned" / s1.name)
    shutil.copyfile(s2,   d / "cleaned" / s2.name)
    for fid, meta in job.files.items():
        if isinstance(meta, dict):
            meta["cleaned_filename"] = meta["name"]
        else:
            meta.cleaned_filename = meta.name
    save_job(job)

    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps({
        "prefix": "TP104-", "axes": ["A", "B", "C", "D", "1", "2", "3"],
    }))

    run_pairs_for_job(job.job_id, pairs=list(job.pairs),
                      prefix="TP104-", axes=["A", "B", "C", "D", "1", "2", "3"])

    # Snapshot all transform.json contents.
    def snapshot():
        loaded = load_job(job.job_id)
        out = {}
        for p in loaded.pairs:
            t = pair_root(job.job_id, p.pair_id) / "transform.json"
            out[p.pair_id] = json.loads(t.read_text()) if t.exists() else None
        return out

    first = snapshot()
    run_pairs_for_job(job.job_id, pairs=list(load_job(job.job_id).pairs),
                      prefix="TP104-", axes=["A", "B", "C", "D", "1", "2", "3"])
    second = snapshot()

    # Transform.json is byte-equivalent on re-run (deterministic least-squares).
    for pid in first:
        a = json.dumps(first[pid], sort_keys=True)
        b = json.dumps(second[pid], sort_keys=True)
        assert a == b, f"pair {pid} transform changed between runs"
