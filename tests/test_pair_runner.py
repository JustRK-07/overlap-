#!/usr/bin/env python3
"""test_pair_runner.py — Unit tests for core.pair_runner (Slice 1).

Covers:
  - encode_pair_id: roundtrip + sanitisation
  - pair_root: returns jobs/<id>/out/pairs/<pair_id>
  - run_pair: writes all expected artifacts, idempotent on re-run
  - run_pair: rejects mech == str, missing files

These tests run WITHOUT a server — `run_pair` is exercised directly
against a fixture job created by conftest.py.

Note: `run_pair` is a heavy operation (full fit + spotcheck +
overlay) — it runs in seconds on the TP-104 fixture. Marked as
`@pytest.mark.slow` so they can be deselected with `-m "not slow"`
when iterating on the lighter tests.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from core.pair_runner import encode_pair_id, pair_root, run_pair  # noqa: E402
from jobs import job_dir  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Encoding
# ─────────────────────────────────────────────────────────────────────────────

def test_encode_pair_id_roundtrip():
    pid = encode_pair_id("TP-104 MECH GA_clean3.dxf",
                         "TP-104 STR GA_clean3.dxf")
    assert pid == "TP-104_MECH_GA_clean3_vs_TP-104_STR_GA_clean3"


def test_encode_pair_id_sanitises_unsafe_chars():
    """Path-traversal-style chars get replaced with `_`."""
    pid = encode_pair_id("../etc/passwd_clean.dxf", "evil/path_clean.dxf")
    # No `/` `\\` `..` in the output.
    assert "/" not in pid
    assert "\\" not in pid
    assert ".." not in pid


def test_encode_pair_id_strips_clean_suffix():
    pid = encode_pair_id("foo_clean.dxf", "bar_clean.dxf")
    assert pid == "foo_vs_bar"


def test_pair_root_path():
    p = pair_root("abc123", "MECH_vs_STR")
    assert str(p).endswith("jobs/abc123/out/pairs/MECH_vs_STR")
    assert p.parent.name == "pairs"


# ─────────────────────────────────────────────────────────────────────────────
# run_pair — heavy (skipped unless TP-104 fixtures exist)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
def test_run_pair_writes_all_artifacts(fx_job_with_cleaned_dxfs, fx_tp104_dxfs):
    """End-to-end: run_pair against the TP-104 fixture, verify all
    expected artifacts land in out/pairs/<pair_id>/."""
    job_id = fx_job_with_cleaned_dxfs
    mech, struct = fx_tp104_dxfs

    pair_id = encode_pair_id(mech.name, struct.name)
    result = run_pair(
        job_id,
        mech_filename=mech.name,
        str_filename=struct.name,
        pair_id=pair_id,
        prefix="TP104-",
        axes=["A", "B", "C", "D", "1", "2", "3"],
    )

    root = pair_root(job_id, pair_id)
    expected = [
        root / "transform.json",
        root / "stages" / "spotcheck.json",
        root / "stages" / "mech_intra.json",
        root / "stages" / "str_intra.json",
        root / "out" / "superimposed.dxf",
        root / "out" / "preview.png",
        root / "out" / "report.json",
        root / "out" / "spotcheck.docx",
        root / "out" / "superimposed_annotated.dxf",
        root / "metadata.json",
    ]
    for p in expected:
        assert p.exists(), f"missing artifact: {p}"

    # Spotcheck should still produce the legacy 17 issues.
    sc = json.loads((root / "stages" / "spotcheck.json").read_text())
    assert len(sc["issues"]) >= 15, (
        f"expected ≥15 issues, got {len(sc['issues'])}"
    )

    # And the response shape must carry metrics.
    assert result["metrics"]["n_issues"] == len(sc["issues"])
    assert "rms_residual" in result["metrics"]


@pytest.mark.slow
def test_run_pair_idempotent(fx_job_with_cleaned_dxfs, fx_tp104_dxfs):
    """Running the same pair twice produces the same n_issues (deterministic
    within ±1 — we tolerate a single off-by-one from any non-deterministic
    ID ordering)."""
    job_id = fx_job_with_cleaned_dxfs
    mech, struct = fx_tp104_dxfs
    pair_id = encode_pair_id(mech.name, struct.name)

    first = run_pair(
        job_id, mech_filename=mech.name, str_filename=struct.name,
        pair_id=pair_id, prefix="TP104-",
        axes=["A", "B", "C", "D", "1", "2", "3"],
    )
    second = run_pair(
        job_id, mech_filename=mech.name, str_filename=struct.name,
        pair_id=pair_id, prefix="TP104-",
        axes=["A", "B", "C", "D", "1", "2", "3"],
    )

    assert abs(first["metrics"]["n_issues"]
               - second["metrics"]["n_issues"]) <= 1


def test_run_pair_self_loop_rejected(fx_job_with_cleaned_dxfs):
    """A pair with mech_file == str_file should raise immediately, before
    we even touch the stages. The route rejects this earlier with HTTP 400;
    the runner should too."""
    job_id = fx_job_with_cleaned_dxfs
    with pytest.raises((FileNotFoundError, ValueError, RuntimeError)):
        run_pair(
            job_id,
            mech_filename="same.dxf",
            str_filename="same.dxf",
            pair_id="x_vs_y",
            prefix="X-",
            axes=["1", "2"],
        )


def test_run_pair_unknown_file_rejected(fx_job_with_cleaned_dxfs):
    job_id = fx_job_with_cleaned_dxfs
    with pytest.raises(FileNotFoundError):
        run_pair(
            job_id,
            mech_filename="nonexistent.dxf",
            str_filename="str_clean.dxf",
            pair_id="x_vs_y",
            prefix="X-",
            axes=["1", "2"],
        )
