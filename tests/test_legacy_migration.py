#!/usr/bin/env python3
"""test_legacy_migration.py — verify _migrate_legacy() + the v2 schema.

Round-trips a fixture v1 `job.json` (flat `files.drawing_a/drawing_b`,
no `pairs`) through `load_job()` and asserts the v2 fields are
synthesised correctly. The 114 on-disk jobs (66 still carrying the
long-removed `color_policy`) are the regression corpus.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import jobs as J  # noqa: E402
from jobs import (  # noqa: E402
    FileMeta,
    JobState,
    Pair,
    _migrate_legacy,
    create_job,
    create_job_legacy,
    load_job,
)


@pytest.fixture
def fx_isolated(monkeypatch, tmp_path):
    """Isolated jobs dir per test."""
    monkeypatch.setattr(J, "JOBS_DIR", tmp_path)
    return tmp_path


def test_migrate_v1_flat_to_v2_synthesises_legacy_pair():
    """v1 shape → one Pair with pair_id='_legacy', legacy_v2=True."""
    v1 = {
        "job_id": "abc", "created_at": "2026-01-01T00:00:00",
        "strategy": "A", "state": "done",
        "files": {
            "drawing_a": {"name": "mech.dxf", "size": 100},
            "drawing_b": {"name": "str.dxf",  "size": 200},
        },
        "color_policy": "auto",      # legacy extra — must be dropped silently
    }
    out = _migrate_legacy(v1)
    assert len(out["pairs"]) == 1
    p = out["pairs"][0]
    assert p["pair_id"] == "_legacy"
    assert p["mech_file_id"] == "drawing_a"
    assert p["str_file_id"]  == "drawing_b"
    assert p["status"] == "done"
    assert out["active_pair_id"] == "_legacy"
    assert out["legacy_v2"] is True


def test_migrate_v2_with_pairs_is_noop():
    """When `pairs` is already present, the migration is a no-op."""
    data = {
        "job_id": "x", "created_at": "x", "state": "queued",
        "files": {"a": {"file_id": "a", "name": "a.dxf", "size": 1, "kind": "mech"}},
        "pairs": [{"pair_id": "p1", "mech_file_id": "a", "str_file_id": "a"}],
    }
    out = _migrate_legacy(dict(data))
    assert out["pairs"][0]["pair_id"] == "p1"
    assert "legacy_v2" not in out


def test_migrate_v2_files_no_pairs_synthesises_first_mech_first_str():
    """v2 file shape with no pairs → synthesise one pair from the first
    MECH and first STR by `kind`."""
    data = {
        "job_id": "x", "created_at": "x", "state": "queued",
        "files": {
            "m1": {"file_id": "m1", "name": "m1.dxf", "size": 1, "kind": "mech"},
            "s1": {"file_id": "s1", "name": "s1.dxf", "size": 1, "kind": "str"},
        },
    }
    out = _migrate_legacy(data)
    assert len(out["pairs"]) == 1
    assert out["pairs"][0]["mech_file_id"] == "m1"
    assert out["pairs"][0]["str_file_id"]  == "s1"
    assert out["legacy_v2"] is True


def test_load_job_returns_dataclass_pairs(fx_isolated):
    """After a round-trip via load_job, pairs is a list of Pair (not dicts)."""
    job = create_job(files=[
        {"name": "a.dxf", "size": 1, "kind": "mech"},
        {"name": "b.dxf", "size": 1, "kind": "str"},
    ])
    loaded = load_job(job.job_id)
    assert isinstance(loaded, JobState)
    assert len(loaded.pairs) == 1
    assert isinstance(loaded.pairs[0], Pair)
    assert loaded.legacy_v2 is False


def test_create_job_legacy_marks_legacy_v2(fx_isolated):
    """create_job_legacy shim writes a _legacy pair immediately and sets legacy_v2=True."""
    job = create_job_legacy(
        drawing_a_name="x.dxf", drawing_a_size=1,
        drawing_b_name="y.dxf", drawing_b_size=1,
        strategy="C",
    )
    assert job.legacy_v2 is True
    assert len(job.pairs) == 1
    assert job.pairs[0].pair_id == "_legacy"
    assert job.pairs[0].strategy == "C"
    # Files keep the legacy flat shape — consumers reading
    # files.drawing_a.name stay bit-equivalent.
    assert job.files["drawing_a"]["name"] == "x.dxf"
    assert job.files["drawing_b"]["name"] == "y.dxf"


def test_create_job_legacy_roundtrip_preserves_files(fx_isolated):
    """load_job(legacy) returns files in the same flat shape it was saved in."""
    job = create_job_legacy(
        drawing_a_name="mech.dxf", drawing_a_size=42,
        drawing_b_name="str.dxf",  drawing_b_size=99,
    )
    loaded = load_job(job.job_id)
    # Flat dict (not FileMeta) is preserved — this is intentional for back-compat.
    assert loaded.files["drawing_a"]["name"] == "mech.dxf"
    assert loaded.files["drawing_a"]["size"] == 42


def test_create_job_v2_roundtrip_returns_file_meta(fx_isolated):
    """load_job(v2) returns files as FileMeta instances."""
    job = create_job(files=[
        {"name": "a.dxf", "size": 5, "kind": "mech"},
        {"name": "b.dxf", "size": 7, "kind": "str"},
    ])
    loaded = load_job(job.job_id)
    fid = list(loaded.files.keys())[0]
    assert isinstance(loaded.files[fid], FileMeta)
    assert loaded.files[fid].kind in ("mech", "str")


def test_create_job_cross_product_auto_pair(fx_isolated):
    """3 files (1 MECH + 2 STR) auto-creates 2 pairs (cross-product)."""
    job = create_job(files=[
        {"name": "m.dxf", "size": 1, "kind": "mech"},
        {"name": "s1.dxf", "size": 1, "kind": "str"},
        {"name": "s2.dxf", "size": 1, "kind": "str"},
    ])
    assert len(job.pairs) == 2
    mech_id = next(fid for fid, m in job.files.items() if m.kind == "mech")
    for pair in job.pairs:
        assert pair.mech_file_id == mech_id
        assert pair.str_file_id != mech_id


def test_load_v1_disk_job_synthesises_pair(fx_isolated, tmp_path):
    """Simulate a v1 on-disk job and confirm load_job() migrates it."""
    v1_job = {
        "job_id": "v1legacy", "created_at": "2026-01-01", "strategy": "A",
        "state": "done",
        "files": {
            "drawing_a": {"name": "old_mech.dxf", "size": 1024},
            "drawing_b": {"name": "old_str.dxf",  "size": 2048},
        },
        "color_policy": "auto",
    }
    d = tmp_path / "v1legacy"
    d.mkdir()
    (d / "job.json").write_text(json.dumps(v1_job))
    loaded = load_job("v1legacy")
    assert loaded is not None
    assert loaded.legacy_v2 is True
    assert len(loaded.pairs) == 1
    assert loaded.pairs[0].pair_id == "_legacy"
    assert loaded.pairs[0].status == "done"
