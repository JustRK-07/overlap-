#!/usr/bin/env python3
"""test_labels.py — stage 3.5a (core.labels) tests.

Verifies the cross-drawing agreement scan + the four JSON outputs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import ezdxf
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from jobs import create_job_legacy, job_dir  # noqa: E402
import core.labels as labels_mod  # noqa: E402


@pytest.fixture
def fx_isolated_jobs(monkeypatch, tmp_path):
    from jobs import JOBS_DIR
    monkeypatch.setattr("jobs.JOBS_DIR", tmp_path)
    return tmp_path


def _write_dxf(path: Path, labels: list[tuple[str, str | None]]):
    """Write a minimal DXF. `labels` is a list of (text, block_name_or_None)."""
    doc = ezdxf.new()
    for text, block_name in labels:
        if block_name:
            blk = doc.blocks.new(name=block_name)
            blk.add_text(text, dxfattribs={"height": 5.0, "insert": (0, 0)})
        else:
            doc.modelspace().add_text(
                text, dxfattribs={"height": 5.0, "insert": (0, 0)}
            )
    doc.saveas(str(path))


def test_scan_writes_all_four_outputs(fx_isolated_jobs):
    """stage 3.5a produces labels.json, cross_drawing_agreement.json,
    protected_handles.json, protected_blocks.json."""
    job = create_job_legacy(
        drawing_a_name="a.dxf", drawing_a_size=10,
        drawing_b_name="b.dxf", drawing_b_size=10,
    )
    d = job_dir(job.job_id)
    (d / "cleaned").mkdir(exist_ok=True)
    _write_dxf(d / "cleaned" / "a.dxf", [
        ("TP104-A", None), ("TP104-B", None), ("TP104-1", None),
    ])
    _write_dxf(d / "cleaned" / "b.dxf", [
        ("TP104-A", "GridLine-A"),
        ("TP104-B", "GridLine-B"),
        ("TP104-1", "GridLine-1"),
        ("ISMB 600", None),  # filtered by beam-section guard
    ])

    summary = labels_mod.scan_labels_for_job(job.job_id)
    stages = d / "stages"
    for fname in ("labels.json", "cross_drawing_agreement.json",
                  "protected_handles.json", "protected_blocks.json"):
        assert (stages / fname).exists(), f"{fname} missing"

    assert summary["agreement_count"] == 3
    # Each agreement text appears in both drawings — so the bucket for
    # the shared prefix has 6 entries (3 texts × 2 drawings).
    assert summary["agreement_by_prefix"]["TP104-"] == 6


def test_agreement_intersects_across_all_drawings(fx_isolated_jobs):
    """Agreement is the intersection across every drawing's text set."""
    job = create_job_legacy(
        drawing_a_name="a.dxf", drawing_a_size=10,
        drawing_b_name="b.dxf", drawing_b_size=10,
    )
    d = job_dir(job.job_id)
    (d / "cleaned").mkdir(exist_ok=True)
    _write_dxf(d / "cleaned" / "a.dxf", [
        ("TP104-A", None), ("TP104-ONLY-A", None),
    ])
    _write_dxf(d / "cleaned" / "b.dxf", [
        ("TP104-A", None), ("TP104-ONLY-B", None),
    ])

    summary = labels_mod.scan_labels_for_job(job.job_id)
    assert summary["agreement_count"] == 1  # only TP104-A is shared
    cross = json.loads((d / "stages" / "cross_drawing_agreement.json").read_text())
    assert cross["agreement_texts"] == ["TP104-A"]


def test_case_insensitive_agreement(fx_isolated_jobs):
    """Text comparison is case-insensitive (tp104-a == TP104-A)."""
    job = create_job_legacy(
        drawing_a_name="a.dxf", drawing_a_size=10,
        drawing_b_name="b.dxf", drawing_b_size=10,
    )
    d = job_dir(job.job_id)
    (d / "cleaned").mkdir(exist_ok=True)
    _write_dxf(d / "cleaned" / "a.dxf", [("TP104-A", None)])
    _write_dxf(d / "cleaned" / "b.dxf", [("tp104-a", None)])

    summary = labels_mod.scan_labels_for_job(job.job_id)
    assert summary["agreement_count"] == 1


def test_protected_handles_keyed_by_dxf_name(fx_isolated_jobs):
    """protected_handles.json is keyed by the DXF filename (not full path)."""
    job = create_job_legacy(
        drawing_a_name="a.dxf", drawing_a_size=10,
        drawing_b_name="b.dxf", drawing_b_size=10,
    )
    d = job_dir(job.job_id)
    (d / "cleaned").mkdir(exist_ok=True)
    _write_dxf(d / "cleaned" / "a.dxf", [("TP104-A", None)])
    _write_dxf(d / "cleaned" / "b.dxf", [("TP104-A", "GridLine-A")])

    labels_mod.scan_labels_for_job(job.job_id)
    handles = json.loads((d / "stages" / "protected_handles.json").read_text())
    blocks = json.loads((d / "stages" / "protected_blocks.json").read_text())
    assert "a.dxf" in handles
    assert "b.dxf" in handles
    assert "b.dxf" in blocks and "GridLine-A" in blocks["b.dxf"]
    # `a.dxf` had modelspace labels only (no block_name), so it's
    # absent from protected_blocks.json — only blocks with names are
    # recorded. The cleaner tolerates the absence.
    assert "a.dxf" not in blocks


def test_no_dxfs_writes_empty_outputs(fx_isolated_jobs):
    """When no DXFs exist (job not yet converted), write empty outputs
    so downstream stages don't crash."""
    job = create_job_legacy(
        drawing_a_name="a.dxf", drawing_a_size=10,
        drawing_b_name="b.dxf", drawing_b_size=10,
    )
    d = job_dir(job.job_id)
    # No cleaned/ dir, no DXFs.
    summary = labels_mod.scan_labels_for_job(job.job_id)
    assert summary == {}
    stages = d / "stages"
    handles = json.loads((stages / "protected_handles.json").read_text())
    assert handles == {}


def test_beam_section_labels_excluded_from_agreement(fx_isolated_jobs):
    """ISMB 600 / M20 never land in the agreement set — they're filtered
    by `_looks_like_beam_section` inside `extract_labels`."""
    job = create_job_legacy(
        drawing_a_name="a.dxf", drawing_a_size=10,
        drawing_b_name="b.dxf", drawing_b_size=10,
    )
    d = job_dir(job.job_id)
    (d / "cleaned").mkdir(exist_ok=True)
    _write_dxf(d / "cleaned" / "a.dxf", [("ISMB 600", None)])
    _write_dxf(d / "cleaned" / "b.dxf", [("ISMB 600", None)])
    summary = labels_mod.scan_labels_for_job(job.job_id)
    assert summary["agreement_count"] == 0
