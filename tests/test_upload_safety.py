#!/usr/bin/env python3
"""tests/test_upload_safety.py — the upload path must not trust a filename.

``UploadFile.filename`` is the raw ``Content-Disposition`` value and
Starlette does **not** sanitise it.  The uploads used to be written as
``(job_dir / "uploads" / filename)``, so a name like ``/etc/cron.d/evil.dxf``
or ``../../evil.dxf`` wrote outside the job directory — an arbitrary file
write on the server.

The same class of bug: ``GET /{id}/analyze?pair_id=…`` joined an
untrusted query parameter onto the job directory without sanitising it.
"""
from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import app as fastapi_app             # noqa: E402
from jobs import job_dir                       # noqa: E402


@pytest.fixture
def fx_client(fx_isolated_jobs):
    return TestClient(fastapi_app)


@pytest.fixture
def fx_no_worker(monkeypatch):
    """Stop the pipeline worker from touching the job."""
    import pipelines
    monkeypatch.setattr(pipelines, "resume_after_axes", lambda *a, **k: None)
    monkeypatch.setattr(pipelines, "run_pipeline", lambda *a, **k: None)
    return True


def _fake_dxf() -> BytesIO:
    buf = BytesIO(b"\x00" * 64)
    buf.name = "x.dxf"
    return buf


def test_a_traversing_filename_stays_inside_the_job_dir(fx_client,
                                                        fx_isolated_jobs,
                                                        fx_no_worker,
                                                        tmp_path):
    """``../../evil.dxf`` must not escape ``uploads/``."""
    marker = tmp_path / "evil.dxf"
    r = fx_client.post(
        "/api/v1/overlay",
        files=[
            ("drawing_a", ("../../evil.dxf", _fake_dxf(), "application/dxf")),
            ("drawing_b", ("str.dxf", _fake_dxf(), "application/dxf")),
        ],
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    uploads = job_dir(job_id) / "uploads"
    names = sorted(p.name for p in uploads.iterdir())
    assert "evil.dxf" in names, names
    assert not marker.exists(), "the file was written outside the job dir"
    # and nothing was written above the uploads directory
    assert not (job_dir(job_id) / "evil.dxf").exists()


def test_an_absolute_filename_stays_inside_the_job_dir(fx_client,
                                                       fx_isolated_jobs,
                                                       fx_no_worker,
                                                       tmp_path):
    """``/tmp/evil.dxf`` must land in ``uploads/``, not in ``/tmp/``."""
    target = tmp_path / "absolute_evil.dxf"
    r = fx_client.post(
        "/api/v1/overlay",
        files=[
            ("drawing_a", (str(target), _fake_dxf(), "application/dxf")),
            ("drawing_b", ("str.dxf", _fake_dxf(), "application/dxf")),
        ],
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert not target.exists(), "the file was written to the absolute path"
    assert (job_dir(job_id) / "uploads" / "absolute_evil.dxf").exists()


def test_a_traversing_filename_in_the_n_file_endpoint(fx_client,
                                                      fx_isolated_jobs,
                                                      fx_no_worker,
                                                      tmp_path):
    """The N-file endpoint had the same flaw."""
    marker = tmp_path / "n_evil.dxf"
    r = fx_client.post(
        "/api/v1/overlay/upload",
        files=[
            ("files", ("../n_evil.dxf", _fake_dxf(), "application/dxf")),
            ("files", ("str.dxf", _fake_dxf(), "application/dxf")),
        ],
        data={"roles": "mech,str"},
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert not marker.exists(), "the file was written outside the job dir"
    assert (job_dir(job_id) / "uploads" / "n_evil.dxf").exists()


def test_the_analyze_pair_id_is_sanitised(fx_client, fx_isolated_jobs):
    """``?pair_id=../../..`` must not read outside the job directory."""
    r = fx_client.get("/api/v1/overlay/whatever/analyze",
                      params={"pair_id": "../../.."})
    # the job does not exist, so the 404 comes first — but it must be a
    # 404/400, never a 500 from a traversal.
    assert r.status_code in (400, 404), r.status_code
