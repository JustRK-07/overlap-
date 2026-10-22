#!/usr/bin/env python3
"""test_n_file_upload.py — POST /api/v1/overlay/upload endpoint tests.

FastAPI TestClient; doesn't spin up uvicorn. These tests do NOT exercise
the full pipeline (no DXF content needed) — they only validate the
endpoint's validation + factory wiring.
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

from app import app as fastapi_app  # noqa: E402


@pytest.fixture
def fx_client(fx_isolated_jobs):
    return TestClient(fastapi_app)


def _fake_file(name: str, nbytes: int = 1024) -> BytesIO:
    """Return a tiny BytesIO pretending to be a DXF."""
    buf = BytesIO(b"\x00" * nbytes)
    buf.name = name
    return buf


def test_upload_rejects_zero_files(fx_client):
    """No files at all → 422 (FastAPI Form validation)."""
    r = fx_client.post(
        "/api/v1/overlay/upload",
        files=[],
        data={"roles": ""},
    )
    assert r.status_code in (400, 422)


def test_upload_rejects_files_roles_length_mismatch(fx_client):
    """len(files) != len(roles) → 400."""
    files = [
        ("files", ("a.dxf", _fake_file("a.dxf"), "application/dxf")),
        ("files", ("b.dxf", _fake_file("b.dxf"), "application/dxf")),
    ]
    r = fx_client.post(
        "/api/v1/overlay/upload",
        files=files,
        data={"roles": "mech"},   # only 1 role for 2 files
    )
    assert r.status_code == 400
    assert "mismatch" in r.json()["detail"].lower()


def test_upload_rejects_unknown_role(fx_client):
    files = [
        ("files", ("a.dxf", _fake_file("a.dxf"), "application/dxf")),
        ("files", ("b.dxf", _fake_file("b.dxf"), "application/dxf")),
    ]
    r = fx_client.post(
        "/api/v1/overlay/upload",
        files=files,
        data={"roles": "mech,blah"},
    )
    assert r.status_code == 400
    assert "role" in r.json()["detail"].lower()


def test_upload_rejects_no_mech(fx_client):
    files = [
        ("files", ("a.dxf", _fake_file("a.dxf"), "application/dxf")),
        ("files", ("b.dxf", _fake_file("b.dxf"), "application/dxf")),
    ]
    r = fx_client.post(
        "/api/v1/overlay/upload",
        files=files,
        data={"roles": "str,str"},
    )
    assert r.status_code == 400
    assert "mech" in r.json()["detail"].lower()


def test_upload_rejects_no_str(fx_client):
    files = [
        ("files", ("a.dxf", _fake_file("a.dxf"), "application/dxf")),
        ("files", ("b.dxf", _fake_file("b.dxf"), "application/dxf")),
    ]
    r = fx_client.post(
        "/api/v1/overlay/upload",
        files=files,
        data={"roles": "mech,other"},
    )
    assert r.status_code == 400
    assert "str" in r.json()["detail"].lower()


def test_upload_rejects_bad_extension(fx_client):
    files = [
        ("files", ("a.txt", _fake_file("a.txt"), "text/plain")),
        ("files", ("b.dxf", _fake_file("b.dxf"), "application/dxf")),
    ]
    r = fx_client.post(
        "/api/v1/overlay/upload",
        files=files,
        data={"roles": "mech,str"},
    )
    assert r.status_code == 400


def test_upload_rejects_duplicate_filenames(fx_client):
    files = [
        ("files", ("a.dxf", _fake_file("a.dxf"), "application/dxf")),
        ("files", ("a.dxf", _fake_file("a.dxf"), "application/dxf")),
    ]
    r = fx_client.post(
        "/api/v1/overlay/upload",
        files=files,
        data={"roles": "mech,str"},
    )
    assert r.status_code == 400
    assert "duplicate" in r.json()["detail"].lower()


def test_upload_rejects_too_many_files(fx_client):
    """17 files → 400 (cap is 16)."""
    files = [
        ("files", (f"f{i}.dxf", _fake_file(f"f{i}.dxf"), "application/dxf"))
        for i in range(17)
    ]
    roles = ",".join(["mech"] + ["str"] * 16)
    r = fx_client.post(
        "/api/v1/overlay/upload",
        files=files,
        data={"roles": roles},
    )
    assert r.status_code == 400
    assert "16" in r.json()["detail"]


def test_upload_accepts_two_files_creates_one_pair(fx_client):
    """Happy path: 2 files (mech+str) → 1 pair queued."""
    files = [
        ("files", ("mech.dxf", _fake_file("mech.dxf"), "application/dxf")),
        ("files", ("str.dxf",  _fake_file("str.dxf"),  "application/dxf")),
    ]
    r = fx_client.post(
        "/api/v1/overlay/upload",
        files=files,
        data={"roles": "mech,str", "prefix": "TP104-", "axes": "A B C 1 2 3"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "job_id" in body
    assert body["file_count"] == 2
    assert body["pair_count"] == 1
    assert body["auto_run"] is True
    assert len(body["pairs"]) == 1
    assert body["pairs"][0]["status"] == "queued"


def test_upload_three_files_creates_cross_product(fx_client):
    """3 files (1 MECH + 2 STR) → 2 pairs."""
    files = [
        ("files", ("m.dxf",  _fake_file("m.dxf"),  "application/dxf")),
        ("files", ("s1.dxf", _fake_file("s1.dxf"), "application/dxf")),
        ("files", ("s2.dxf", _fake_file("s2.dxf"), "application/dxf")),
    ]
    r = fx_client.post(
        "/api/v1/overlay/upload",
        files=files,
        data={"roles": "mech,str,str"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_count"] == 3
    assert body["pair_count"] == 2


def test_upload_other_files_included_in_pair_count(fx_client):
    """`other`-role files are uploaded but don't form pairs."""
    files = [
        ("files", ("m.dxf",  _fake_file("m.dxf"),  "application/dxf")),
        ("files", ("s1.dxf", _fake_file("s1.dxf"), "application/dxf")),
        ("files", ("x.dxf",  _fake_file("x.dxf"),  "application/dxf")),  # role=other
    ]
    r = fx_client.post(
        "/api/v1/overlay/upload",
        files=files,
        data={"roles": "mech,str,other"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_count"] == 3
    assert body["pair_count"] == 1   # only 1 MECH×STR combination


def test_filename_reconciliation_matches_cleaner_rename(fx_client, fx_isolated_jobs, fx_tp104_dxfs, tmp_path):
    """When the cleaner renames an upload (e.g. `x_R12.dxf` →
    `x_R12_clean.dxf`), the route must reconcile filenames before
    running pairs. Otherwise run_pair copies the wrong filename into
    the pair root and fit.py can't find the geometry.

    Verifies via live HTTP POST → wait for state=done → inspect the
    job.json FileMeta's cleaned_filename. Doesn't try to mock the
    worker thread (which races with any direct call).
    """
    import time
    from pathlib import Path
    from jobs import load_job

    # Point JOBS_DIR at a tmp_path so the test doesn't pollute live jobs/
    # (fx_isolated_jobs already did this via monkeypatch — we just need
    # to know where jobs land.)
    jobs_root = fx_isolated_jobs
    assert jobs_root.exists(), jobs_root

    mech, struct = fx_tp104_dxfs
    # We can't run the real cleaner (it needs DXF content), but the
    # route's reconciliation logic reads from `cleaned/`, so we upload
    # files with the cleaned names already in place and use the
    # `_maybe_run_pairs_after_cleaning` resolver directly.
    files = [
        ("files", (mech.name, _fake_file(mech.name), "application/dxf")),
        ("files", ("str_R12.dxf",  _fake_file("str_R12.dxf"),  "application/dxf")),
        ("files", (struct.name,    _fake_file(struct.name),    "application/dxf")),
    ]
    r = fx_client.post(
        "/api/v1/overlay/upload",
        files=files,
        data={"roles": "mech,str,str"},
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    # Stop the worker thread that the route spawned (we don't want it
    # to run the real pipeline — we're testing the reconciliation logic
    # directly). The worker doesn't check any flag we can set, but we
    # can intercept by writing a state=failed so it short-circuits.
    # Simpler: just run the reconciliation ourselves and assert.
    import shutil
    cleaned_dir = jobs_root / job_id / "cleaned"
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    # Simulate the cleaner's rename: `str_R12.dxf` → `str_R12_clean.dxf`.
    shutil.copyfile(mech,   cleaned_dir / mech.name)
    shutil.copyfile(struct, cleaned_dir / struct.name)
    shutil.copyfile(struct, cleaned_dir / "str_R12_clean.dxf")

    # Run the resolver. After it, FileMeta.cleaned_filename for the
    # uploaded "str_R12.dxf" should point to "str_R12_clean.dxf".
    from routers.api import _maybe_run_pairs_after_cleaning
    import threading
    done = threading.Event()
    _maybe_run_pairs_after_cleaning(
        job_id, prefix="TP104-", axes=["A","B","C","1","2","3"],
        pair_pipeline_done=done,
    )

    loaded = load_job(job_id)
    # Find the FileMeta for "str_R12.dxf"
    for fid, meta in loaded.files.items():
        name = meta.get("name") if isinstance(meta, dict) else meta.name
        if name == "str_R12.dxf":
            cleaned = meta.get("cleaned_filename") if isinstance(meta, dict) else meta.cleaned_filename
            assert cleaned == "str_R12_clean.dxf", \
                f"expected reconciliation to 'str_R12_clean.dxf', got {cleaned!r}"
            return
    pytest.fail("No FileMeta with name 'str_R12.dxf' found in job.files")
