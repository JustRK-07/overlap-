#!/usr/bin/env python3
"""test_mismatch_stage.py — PLAN_MISMATCH §6.4, sub-step 8b.5.

The mismatch detection engine existed but was not reachable: nothing ran it,
nothing wrote ``stages/mismatch.json``, nothing drew the QA markers and
nothing merged the findings.  These tests drive the stage end to end on the
synthetic fixture and pin the contract the plan states:

* the stage writes ``stages/mismatch.json``;
* it is **non-fatal** — a failure produces ``status="error"``, never an
  empty list under ``status="ok"``;
* a disabled policy produces ``status="disabled"``, not "0 findings";
* the ``QA|MISMATCH*`` markers land in ``out/superimposed.dxf`` and the
  drawing is **idempotent**;
* ``out/findings.{json,csv,docx}`` are written;
* ``report.json`` carries a ``mismatch`` section.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import ezdxf
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import synth                                   # noqa: E402
from core import mismatch as mismatch_mod      # noqa: E402

PREFIX = "TP104-"
AXES = ["A", "B", "C", "D", "1", "2", "3"]


def _prepared_job(fx_synth_job, opts=None):
    """Stages 1-7 plus fit + overlay — everything the mismatch stage needs."""
    import pipelines
    from core import fit, overlay
    from jobs import job_dir

    job_id, pair = fx_synth_job(opts or synth.MISMATCH_HARD)
    pipelines.run_pipeline(job_id)
    d = job_dir(job_id)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": PREFIX, "axes": AXES}))
    fit.run(job_id)
    overlay.run(job_id)
    return job_id, d, pair


def _mismatch_layers(doc):
    """The entities on every ``QA|MISMATCH*`` layer, by layer name."""
    out = {}
    for e in doc.modelspace():
        layer = e.dxf.layer
        if layer.startswith("QA|MISMATCH"):
            out.setdefault(layer, []).append(e)
    return out


# ── the stage ──────────────────────────────────────────────────────────────

def test_the_stage_writes_the_diagnostic_json(fx_synth_job):
    """``stages/mismatch.json`` carries the policy, units, coverage, findings."""
    job_id, d, _pair = _prepared_job(fx_synth_job)

    mismatch_mod.run(job_id)

    path = d / "stages" / "mismatch.json"
    assert path.exists(), "the stage must write stages/mismatch.json"
    payload = json.loads(path.read_text())
    assert payload["status"] == "ok"
    assert payload["source_docs"] == "converted"
    assert payload["policy_hash"]
    assert payload["units"]["mm_per_unit"] == 1.0
    assert "coverage" in payload
    assert isinstance(payload["findings"], list)
    assert payload["summary"]["total"] == len(payload["findings"])


def test_the_stage_is_not_fatal(fx_synth_job, monkeypatch):
    """A crash produces ``status="error"`` — never an empty "ok" result."""
    job_id, d, _pair = _prepared_job(fx_synth_job)

    def _boom(*args, **kwargs):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(mismatch_mod, "detect", _boom)

    mismatch_mod.run(job_id)          # must NOT raise

    findings = json.loads((d / "out" / "findings.json").read_text())
    assert findings["status"] == "error"
    assert "detector exploded" in findings["message"]
    assert findings["findings"] == [], "an error must not look like zero findings"

    diag = json.loads((d / "stages" / "mismatch.json").read_text())
    assert diag["status"] == "error"


def test_a_disabled_policy_says_disabled_not_zero(fx_synth_job):
    """``policy.enabled = False`` → ``status="disabled"``."""
    job_id, d, _pair = _prepared_job(fx_synth_job)
    (d / "mismatch_policy.json").write_text(json.dumps({"enabled": False}))

    mismatch_mod.run(job_id)

    diag = json.loads((d / "stages" / "mismatch.json").read_text())
    assert diag["status"] == "disabled"
    findings = json.loads((d / "out" / "findings.json").read_text())
    assert findings["status"] == "disabled"
    assert findings["findings"] == []


def test_the_stage_reads_the_converted_files(fx_synth_job):
    """The detector reads ``converted/``, not the cleaned files.

    The MECH cleaner keeps only ``A$C``/``RTR`` INSERTs, so the bolt and
    saddle blocks are gone from ``cleaned/`` — reading the cleaned files
    would find nothing.
    """
    job_id, d, _pair = _prepared_job(fx_synth_job)
    assert (d / "converted" / "zz_plant.dxf").exists()

    mismatch_mod.run(job_id)

    diag = json.loads((d / "stages" / "mismatch.json").read_text())
    assert diag["source_docs"] == "converted"
    # the STR features the engine extracts must come from the STR drawing
    assert diag["features"].get("STR"), "no STR features were extracted"


# ── the QA markers ─────────────────────────────────────────────────────────

def test_the_markers_are_drawn_into_the_superimposed_dxf(fx_synth_job):
    """The findings land on the ``QA|MISMATCH*`` layers."""
    job_id, d, _pair = _prepared_job(fx_synth_job)

    mismatch_mod.run(job_id)

    doc = ezdxf.readfile(str(d / "out" / "superimposed.dxf"))
    layers = _mismatch_layers(doc)
    assert layers, "no QA|MISMATCH* entities were drawn"

    # the four layers exist with the plan's ACI colours
    expected = {"QA|MISMATCH_CRITICAL": 1, "QA|MISMATCH_WARNING": 30,
                "QA|MISMATCH_INFO": 8, "QA|MISMATCH_REF": 4}
    for name, aci in expected.items():
        assert doc.layers.has_entry(name), f"{name} is missing"
        assert doc.layers.get(name).dxf.color == aci

    # the file still audits clean
    assert doc.audit().has_errors is False


def test_the_markers_are_idempotent(fx_synth_job):
    """Re-running the stage must not stack the markers."""
    job_id, d, _pair = _prepared_job(fx_synth_job)

    mismatch_mod.run(job_id)
    first = len(_mismatch_layers(ezdxf.readfile(str(d / "out" / "superimposed.dxf"))))

    mismatch_mod.run(job_id)
    second = len(_mismatch_layers(ezdxf.readfile(str(d / "out" / "superimposed.dxf"))))

    assert first > 0
    assert second == first, "the markers must be replaced, not accumulated"


def test_the_markers_carry_their_id_in_xdata(fx_synth_job):
    """Each marker carries its finding id / uid / kind in XDATA."""
    job_id, d, _pair = _prepared_job(fx_synth_job)

    mismatch_mod.run(job_id)

    doc = ezdxf.readfile(str(d / "out" / "superimposed.dxf"))
    circles = [e for layer, ents in _mismatch_layers(doc).items()
               for e in ents if e.dxftype() == "CIRCLE"]
    assert circles, "no marker circles were drawn"
    assert doc.appids.has_entry("OVERLAPQA")
    xdata = circles[0].get_xdata("OVERLAPQA")
    assert xdata and len(xdata) == 3
    assert all(tag == 1000 for tag, _ in xdata)


# ── the merged findings ────────────────────────────────────────────────────

def test_the_findings_are_written(fx_synth_job):
    """``out/findings.{json,csv,docx}`` are written by the merger."""
    job_id, d, _pair = _prepared_job(fx_synth_job)

    mismatch_mod.run(job_id)
    import core.findings as findings_mod
    findings_mod.run(job_id)

    out = d / "out"
    assert (out / "findings.json").exists()
    assert (out / "findings.csv").exists()
    assert (out / "findings.docx").exists()

    payload = json.loads((out / "findings.json").read_text())
    assert payload["status"] == "ok"
    assert "summary" in payload and "coverage" in payload
    assert payload["generated_at"]

    import csv
    with (out / "findings.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(payload["findings"])


def test_the_report_carries_a_mismatch_section(fx_synth_job):
    """``report.json["mismatch"]`` has the plan's keys."""
    job_id, d, _pair = _prepared_job(fx_synth_job)

    mismatch_mod.run(job_id)
    import core.package as package_mod
    package_mod.run(job_id)

    report = json.loads((d / "out" / "report.json").read_text())
    section = report["mismatch"]
    for key in ("enabled", "status", "policy_hash", "units", "source_docs",
                "features", "coverage", "summary", "systematic",
                "fit_context", "top", "warnings", "timings_s", "truncated"):
        assert key in section, f"report['mismatch'] is missing {key}"
    assert section["status"] == "ok"


def test_the_spotcheck_json_has_a_superseded_key(fx_synth_job):
    """``spotcheck.json`` carries ``superseded`` (PLAN_MISMATCH §5)."""
    job_id, d, _pair = _prepared_job(fx_synth_job)

    mismatch_mod.run(job_id)
    import core.spotcheck as spotcheck_mod
    spotcheck_mod.run(job_id)

    payload = json.loads((d / "stages" / "spotcheck.json").read_text())
    assert "superseded" in payload
    assert isinstance(payload["superseded"], list)


# ── the whole pipeline ──────────────────────────────────────────────────────

def test_the_pipeline_runs_the_mismatch_and_findings_stages(fx_synth_job):
    """``resume_after_axes`` must run fit → overlay → mismatch → … → findings.

    This is the integration proof: the stages are wired into the pipeline,
    not just callable by hand.
    """
    import pipelines
    from jobs import job_dir, load_job

    job_id, _pair = fx_synth_job(synth.MISMATCH_HARD)
    pipelines.run_pipeline(job_id)
    d = job_dir(job_id)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": PREFIX, "axes": AXES}))

    pipelines.resume_after_axes(job_id)

    job = load_job(job_id)
    assert job.state == "done", job.error

    assert (d / "stages" / "mismatch.json").exists()
    assert (d / "out" / "findings.json").exists()
    assert (d / "out" / "findings.csv").exists()

    report = json.loads((d / "out" / "report.json").read_text())
    assert report["mismatch"]["status"] == "ok"
    assert report["mismatch"]["summary"]["total"] >= 1


def test_a_failed_mismatch_stage_does_not_fail_the_job(fx_synth_job,
                                                      monkeypatch):
    """The mismatch stage is non-fatal — the job still finishes."""
    import pipelines
    from jobs import job_dir, load_job

    job_id, _pair = fx_synth_job(synth.MISMATCH_HARD)
    pipelines.run_pipeline(job_id)
    d = job_dir(job_id)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": PREFIX, "axes": AXES}))

    def _boom(*args, **kwargs):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(mismatch_mod, "detect", _boom)

    pipelines.resume_after_axes(job_id)

    job = load_job(job_id)
    assert job.state == "done", job.error
    findings = json.loads((d / "out" / "findings.json").read_text())
    assert findings["status"] == "error"
