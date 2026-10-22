#!/usr/bin/env python3
"""test_analyze_endpoints.py — FastAPI TestClient tests for /analyze.

Exercises the actual router wiring: path parsing, status codes,
response shape. Doesn't spin up uvicorn.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import app as fastapi_app  # noqa: E402
from core.pair_runner import encode_pair_id, run_pair  # noqa: E402
from jobs import job_dir  # noqa: E402


@pytest.fixture
def fx_client(fx_isolated_jobs):
    return TestClient(fastapi_app)


def test_get_analyze_404_on_unknown_job(fx_client, fx_isolated_jobs):
    r = fx_client.get("/api/v1/overlay/ghost/analyze")
    assert r.status_code == 404


def test_get_analyze_404_when_artifacts_missing(fx_client, fx_job_with_cleaned_dxfs):
    """A job that hasn't run stages 6/7.5 yet has no transform.json →
    404, not 500."""
    job_id = fx_job_with_cleaned_dxfs
    r = fx_client.get(f"/api/v1/overlay/{job_id}/analyze")
    assert r.status_code == 404
    assert "transform.json" in r.json()["detail"]


@pytest.mark.slow
def test_get_analyze_returns_intersections(fx_client, fx_job_with_cleaned_dxfs, fx_tp104_dxfs):
    """End-to-end: run pair_runner, then hit /analyze and verify the
    response shape matches the plan."""
    job_id = fx_job_with_cleaned_dxfs
    mech, struct = fx_tp104_dxfs
    pair_id = encode_pair_id(mech.name, struct.name)
    run_pair(
        job_id, mech_filename=mech.name, str_filename=struct.name,
        pair_id=pair_id, prefix="TP104-",
        axes=["A", "B", "C", "D", "1", "2", "3"],
    )

    # Multi-pair layout → /analyze without pair_id returns {pairs: [...]}.
    r = fx_client.get(f"/api/v1/overlay/{job_id}/analyze")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "pairs" in body
    assert len(body["pairs"]) == 1
    pair = body["pairs"][0]
    assert pair["pair_id"] == pair_id
    assert pair["mech_file"] == mech.name
    assert pair["str_file"] == struct.name
    assert all(0.0 <= ix["normalized_depth"] <= 1.0
               for ix in pair["intersections"])


@pytest.mark.slow
def test_get_analyze_specific_pair(fx_client, fx_job_with_cleaned_dxfs, fx_tp104_dxfs):
    """?pair_id=X returns just that pair's payload (not wrapped)."""
    job_id = fx_job_with_cleaned_dxfs
    mech, struct = fx_tp104_dxfs
    pair_id = encode_pair_id(mech.name, struct.name)
    run_pair(
        job_id, mech_filename=mech.name, str_filename=struct.name,
        pair_id=pair_id, prefix="TP104-",
        axes=["A", "B", "C", "D", "1", "2", "3"],
    )

    r = fx_client.get(
        f"/api/v1/overlay/{job_id}/analyze?pair_id={pair_id}",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # No "pairs" wrapper — single payload directly.
    assert body["pair_id"] == pair_id
    assert "intersections" in body


def test_get_analyze_specific_pair_404_when_missing(fx_client, fx_job_with_cleaned_dxfs):
    """?pair_id=X where X has no transform.json → 404."""
    job_id = fx_job_with_cleaned_dxfs
    r = fx_client.get(
        f"/api/v1/overlay/{job_id}/analyze?pair_id=ghost",
    )
    assert r.status_code == 404
