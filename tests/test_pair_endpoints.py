#!/usr/bin/env python3
"""test_pair_endpoints.py — FastAPI TestClient tests for the /pairs routes.

Uses `fastapi.testclient.TestClient` so we exercise the actual router
wiring (path parsing, status codes, response shape) without spinning
up uvicorn.

The TestClient reuses the same `app` instance uvicorn serves in
production. The `fx_isolated_jobs` fixture has already redirected
`jobs.JOBS_DIR` to `tmp_path`, so any state we create lives only in
that tmp dir.
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
from jobs import job_dir  # noqa: E402


@pytest.fixture
def fx_client(fx_isolated_jobs):
    """A FastAPI TestClient with jobs.JOBS_DIR redirected to tmp_path.

    Imports `app.app` lazily so the `fx_isolated_jobs` monkeypatch of
    `jobs.JOBS_DIR` happens first — important because `app` reads
    JOBS_DIR at import time when it constructs the static mount path.
    """
    return TestClient(fastapi_app)


# ─────────────────────────────────────────────────────────────────────────────
# GET /{job_id}/pairs — list pairs
# ─────────────────────────────────────────────────────────────────────────────

def test_list_pairs_empty(fx_client, fx_isolated_jobs):
    """A fresh job with no pairs yet → `{pairs: []}`."""
    from jobs import create_job_legacy, save_job, job_dir

    # Quick route to a fresh job_id without uploading files.
    job = create_job_legacy(
        strategy="A",
        drawing_a_name="a.dxf", drawing_a_size=10,
        drawing_b_name="b.dxf", drawing_b_size=10,
    )
    d = job_dir(job.job_id)
    d.mkdir(parents=True, exist_ok=True)
    job.state = "done"
    save_job(job)

    r = fx_client.get(f"/api/v1/overlay/{job.job_id}/pairs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"job_id": job.job_id, "pairs": []}


def test_list_pairs_404_on_unknown_job(fx_client, fx_isolated_jobs):
    r = fx_client.get("/api/v1/overlay/nonexistent/pairs")
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# POST /{job_id}/pairs — validation
# ─────────────────────────────────────────────────────────────────────────────

def test_post_pairs_self_loop_rejected(fx_client, fx_job_with_cleaned_dxfs):
    job_id = fx_job_with_cleaned_dxfs
    same = "mech_clean.dxf"
    r = fx_client.post(
        f"/api/v1/overlay/{job_id}/pairs",
        json={"mech_file": same, "str_file": same,
              "prefix": "TP104-", "axes": ["A", "1"]},
    )
    assert r.status_code == 400
    assert "differ" in r.json()["detail"]


def test_post_pairs_unknown_file_rejected(fx_client, fx_job_with_cleaned_dxfs):
    job_id = fx_job_with_cleaned_dxfs
    r = fx_client.post(
        f"/api/v1/overlay/{job_id}/pairs",
        json={"mech_file": "ghost.dxf", "str_file": "str_clean.dxf",
              "prefix": "TP104-", "axes": ["A", "1"]},
    )
    assert r.status_code == 400


def test_post_pairs_unknown_job_rejected(fx_client, fx_isolated_jobs):
    r = fx_client.post(
        "/api/v1/overlay/nonexistent/pairs",
        json={"mech_file": "a.dxf", "str_file": "b.dxf",
              "prefix": "X-", "axes": ["1", "2"]},
    )
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# GET /{job_id}/pairs/{pair_id}/{artifact} — path traversal
# ─────────────────────────────────────────────────────────────────────────────

def test_get_pair_artifact_path_traversal_blocked(fx_client, fx_job_with_cleaned_dxfs):
    """`..` in the URL must be rejected with 400, not a path-traversal."""
    job_id = fx_job_with_cleaned_dxfs
    r = fx_client.get(
        f"/api/v1/overlay/{job_id}/pairs/..%2F..%2Fetc/transform.json",
    )
    # Either 400 (path-traversal block) or 404 (route doesn't match) is
    # acceptable — what we MUST NOT see is a 200 that streams
    # /etc/transform.json.
    assert r.status_code in (400, 404)
    if r.status_code == 400:
        # When 400, the detail should mention traversal / separators.
        assert any(s in r.json()["detail"].lower()
                   for s in ("path", "traversal", "separator"))


def test_get_pair_artifact_unknown_artifact(fx_client, fx_job_with_cleaned_dxfs):
    job_id = fx_job_with_cleaned_dxfs
    r = fx_client.get(
        f"/api/v1/overlay/{job_id}/pairs/MECH_vs_STR/not-a-real-artifact.txt",
    )
    assert r.status_code == 400


def test_get_pair_artifact_404_when_pair_missing(fx_client, fx_job_with_cleaned_dxfs):
    job_id = fx_job_with_cleaned_dxfs
    r = fx_client.get(
        f"/api/v1/overlay/{job_id}/pairs/no_such_pair/transform.json",
    )
    assert r.status_code == 404
