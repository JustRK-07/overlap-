#!/usr/bin/env python3
"""tests/test_mismatch_status.py — the check says what it actually did.

The mismatch check used to answer "ok" whenever it produced no findings.
That is a lie in the common case: a drawing with no recognisable structure
produced no findings **because nothing was compared**, and "ok" reads as
"the two drawings agree".

So the status is now a statement about the *comparison*:

``passed``               the drawings were really compared and agreed
``discrepancies_found``  they were compared and something was found
``insufficient_coverage``nothing was comparable — never ``passed``
``blocked``              the fit behind the comparison is not trusted
``failed``               the check raised
``disabled``             the policy turned it off

Six scenes, one per outcome, plus the per-family counts that justify them.
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
from core.mismatch import detect, status as status_mod   # noqa: E402
from core.transform2d import Affine2D           # noqa: E402

PREFIX = "TP104-"
AXES = ["A", "B", "C", "D", "1", "2", "3"]


@pytest.fixture
def fx_client(fx_isolated_jobs):
    """A TestClient with ``jobs.JOBS_DIR`` redirected to tmp_path."""
    from fastapi.testclient import TestClient
    from app import app as fastapi_app
    return TestClient(fastapi_app)


# ─────────────────────────────────────────────────────────────────────────────
# scenes
# ─────────────────────────────────────────────────────────────────────────────

def _doc() -> "ezdxf.document.Drawing":
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4          # millimetres
    return doc


def _bolt(doc, x: float, y: float, r: float = 10.0) -> None:
    """A MECH bolt circle — the ``mech_bolt`` family."""
    if "BOLT" not in doc.layers:
        doc.layers.add("BOLT")
    doc.modelspace().add_circle((x, y), r, dxfattribs={"layer": "BOLT"})


def _hole(doc, x: float, y: float, r: float = 10.0) -> None:
    """An STR hole circle — the ``str_hole`` family."""
    if "HOLE" not in doc.layers:
        doc.layers.add("HOLE")
    doc.modelspace().add_circle((x, y), r, dxfattribs={"layer": "HOLE"})


def _decoy(doc, x: float, y: float, r: float = 10.0) -> None:
    """A circle on a layer no family recognises — geometry, but no family."""
    if "DECOR" not in doc.layers:
        doc.layers.add("DECOR")
    doc.modelspace().add_circle((x, y), r, dxfattribs={"layer": "DECOR"})


def _scene(tmp_path, *, mech=None, str_=None) -> tuple[Path, Path]:
    """Write the two drawings of a scene and return their paths."""
    mdoc = _doc()
    sdoc = _doc()
    for fn in (mech or ()):
        fn(mdoc)
    for fn in (str_ or ()):
        fn(sdoc)
    mpath = tmp_path / "mech.dxf"
    spath = tmp_path / "str.dxf"
    mdoc.saveas(mpath)
    sdoc.saveas(spath)
    return mpath, spath


#: A fit context that says the alignment is good.
GOOD_FIT = {
    "quality": {"status": "ok"},
    "rms_residual": 0.0,
    "correspondences": [
        {"label": "A-1", "str": [0.0, 0.0], "mech": [0.0, 0.0],
         "residual": 0.0, "loo_residual": 0.0, "inlier": True},
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# 1 · no STR entities
# ─────────────────────────────────────────────────────────────────────────────

def test_no_str_entities_is_insufficient_coverage(tmp_path):
    """The STR drawing is empty: nothing was compared, so nothing passed."""
    mpath, spath = _scene(tmp_path, mech=[lambda d: _bolt(d, 0.0, 0.0)])

    report = detect(mpath, spath, Affine2D(), fit_ctx=GOOD_FIT)

    assert report.status == status_mod.INSUFFICIENT_COVERAGE
    assert report.status != status_mod.PASSED
    assert "not really compared" in report.reason
    # the bolt was seen, but it had nothing to be compared with
    assert report.family_coverage["mech_bolt"]["extracted"] == 1
    assert report.family_coverage["mech_bolt"]["eligible"] == 0
    assert report.family_coverage["mech_bolt"]["matched"] == 0
    assert report.family_coverage["mech_bolt"]["unmatched"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 2 · no counterpart
# ─────────────────────────────────────────────────────────────────────────────

def test_no_counterpart_is_insufficient_coverage(tmp_path):
    """The STR drawing has geometry, but none of it is a counterpart."""
    mpath, spath = _scene(tmp_path,
                          mech=[lambda d: _bolt(d, 0.0, 0.0)],
                          str_=[lambda d: _decoy(d, 0.0, 0.0)])

    report = detect(mpath, spath, Affine2D(), fit_ctx=GOOD_FIT)

    assert report.status == status_mod.INSUFFICIENT_COVERAGE
    assert report.status != status_mod.PASSED
    # the STR contributed no hole, so the leg had an empty side
    assert report.family_coverage["str_hole"]["extracted"] == 0
    assert report.family_coverage["str_hole"]["eligible"] == 0
    assert report.family_coverage["mech_bolt"]["eligible"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3 · an exact match
# ─────────────────────────────────────────────────────────────────────────────

def test_an_exact_match_is_passed(tmp_path):
    """The bolt and the hole coincide: compared, and in agreement."""
    mpath, spath = _scene(tmp_path,
                          mech=[lambda d: _bolt(d, 0.0, 0.0)],
                          str_=[lambda d: _hole(d, 0.0, 0.0)])

    report = detect(mpath, spath, Affine2D(), fit_ctx=GOOD_FIT)

    assert report.status == status_mod.PASSED
    assert not report.findings
    assert "compared items on both sides" in report.reason

    # the per-family counts: one of each, compared, and matched
    assert report.family_coverage["mech_bolt"] == {
        "extracted": 1, "eligible": 1, "matched": 1, "unmatched": 0}
    assert report.family_coverage["str_hole"] == {
        "extracted": 1, "eligible": 1, "matched": 1, "unmatched": 0}
    # …and the per-check counters are still there
    assert report.coverage["bolts"]["items"] == 1
    assert report.coverage["bolts"]["matched"] == 1


def test_a_real_discrepancy_is_reported_as_such(tmp_path):
    """The same scene with the hole 30 mm away: compared, and it found something."""
    mpath, spath = _scene(tmp_path,
                          mech=[lambda d: _bolt(d, 0.0, 0.0)],
                          str_=[lambda d: _hole(d, 30.0, 0.0)])

    report = detect(mpath, spath, Affine2D(), fit_ctx=GOOD_FIT)

    assert report.status == status_mod.DISCREPANCIES_FOUND
    assert report.findings
    assert "found" in report.reason
    # the bolt WAS compared against the hole — that is what the coverage
    # says; what the comparison found is the finding.
    assert report.family_coverage["mech_bolt"]["eligible"] == 1
    assert report.family_coverage["mech_bolt"]["matched"] == 1
    assert report.family_coverage["mech_bolt"]["unmatched"] == 0
    assert report.findings[0].kind == "BOLT_HOLE_OFFSET"


# ─────────────────────────────────────────────────────────────────────────────
# 4 · a weak (forced) fit
# ─────────────────────────────────────────────────────────────────────────────

def test_a_forced_fit_blocks_the_check(tmp_path):
    """A forced fit cannot support a comparison, so the check is blocked."""
    mpath, spath = _scene(tmp_path,
                          mech=[lambda d: _bolt(d, 0.0, 0.0)],
                          str_=[lambda d: _hole(d, 0.0, 0.0)])

    weak_fit = {**GOOD_FIT, "quality": {"status": "forced"}}
    report = detect(mpath, spath, Affine2D(), fit_ctx=weak_fit)

    assert report.status == status_mod.BLOCKED
    assert report.status != status_mod.PASSED
    assert "forced" in report.reason
    assert "cannot be relied on" in report.reason


def test_an_untrusted_fit_blocks_the_check(tmp_path):
    """A fit whose quality is "weak" is untrusted in the same way."""
    mpath, spath = _scene(tmp_path,
                          mech=[lambda d: _bolt(d, 0.0, 0.0)],
                          str_=[lambda d: _hole(d, 0.0, 0.0)])

    weak_fit = {**GOOD_FIT, "quality": {"status": "weak"}}
    report = detect(mpath, spath, Affine2D(), fit_ctx=weak_fit)

    assert report.status == status_mod.BLOCKED


def test_confirming_with_force_is_always_blocked(fx_client, fx_synth_job):
    """``POST /axes/confirm {"force": true}`` → the check is ``blocked``.

    Always.  A forced fit cannot support a comparison, so the check must
    not say ``passed`` — not even when it found nothing, and not even
    when the two drawings really do agree.  "We could not tell" and
    "they agree" are different answers.
    """
    import pipelines
    import time
    from jobs import job_dir, load_job, save_job

    # The plain pair: no defects anywhere, so the check has nothing to
    # report — which is exactly the case where a forced fit must not be
    # allowed to say "passed".
    job_id, pair = fx_synth_job(synth.LEGACY_COMPATIBLE)
    pipelines.run_pipeline(job_id)
    d = job_dir(job_id)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": PREFIX, "axes": AXES}))

    # The user confirms the axes with force — they said not to trust the
    # fit, whatever its gates said.  The confirm endpoint resumes the
    # pipeline in a worker, so wait for it.
    job = load_job(job_id)
    job.state = "awaiting_axes"
    save_job(job)
    rc = fx_client.post(f"/api/v1/overlay/{job_id}/axes/confirm",
                        json={"force": True})
    assert rc.status_code == 200, rc.text

    deadline = time.time() + 120
    while time.time() < deadline:
        state = load_job(job_id).state
        if state in ("done", "failed"):
            break
        time.sleep(0.2)
    assert load_job(job_id).state == "done", load_job(job_id).message

    payload = json.loads((d / "out" / "findings.json").read_text())
    assert payload["status"] == status_mod.BLOCKED
    assert payload["status"] != status_mod.PASSED
    # …even though the check itself found nothing to report.
    assert payload["findings"] == []
    assert "forced" in payload["reason"]


# ─────────────────────────────────────────────────────────────────────────────
# 5 · the detector raises
# ─────────────────────────────────────────────────────────────────────────────

def test_a_detector_exception_is_failed(fx_synth_job, monkeypatch):
    """The check raised: ``failed``, never "0 findings"."""
    import json as _json

    from core import mismatch as mismatch_mod
    from jobs import job_dir

    job_id, pair = fx_synth_job(synth.MISMATCH_HARD)
    import pipelines
    pipelines.run_pipeline(job_id)
    d = job_dir(job_id)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(_json.dumps(
        {"prefix": PREFIX, "axes": AXES}))
    from core import fit, overlay
    fit.run(job_id)
    overlay.run(job_id)

    def _boom(*args, **kwargs):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(mismatch_mod, "detect", _boom)
    mismatch_mod.run(job_id)

    diag = _json.loads((d / "stages" / "mismatch.json").read_text())
    assert diag["status"] == status_mod.FAILED
    assert "detector exploded" in diag["reason"]
    assert diag["findings"] == []

    findings = _json.loads((d / "out" / "findings.json").read_text())
    assert findings["status"] == status_mod.FAILED
    assert findings["findings"] == []


# ─────────────────────────────────────────────────────────────────────────────
# 6 · every check disabled
# ─────────────────────────────────────────────────────────────────────────────

def test_all_checks_disabled_is_disabled(fx_synth_job):
    """The policy turned the check off: ``disabled``, not "no findings"."""
    import json as _json

    from core import mismatch as mismatch_mod
    from jobs import job_dir

    job_id, pair = fx_synth_job(synth.MISMATCH_HARD)
    import pipelines
    pipelines.run_pipeline(job_id)
    d = job_dir(job_id)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(_json.dumps(
        {"prefix": PREFIX, "axes": AXES}))
    from core import fit, overlay
    fit.run(job_id)
    overlay.run(job_id)

    (d / "mismatch_policy.json").write_text(_json.dumps({"enabled": False}))
    mismatch_mod.run(job_id)

    diag = _json.loads((d / "stages" / "mismatch.json").read_text())
    assert diag["status"] == status_mod.DISABLED
    assert diag["reason"]
    assert diag["findings"] == []

    findings = _json.loads((d / "out" / "findings.json").read_text())
    assert findings["status"] == status_mod.DISABLED
    assert findings["findings"] == []


# ─────────────────────────────────────────────────────────────────────────────
# the headline is the cross-drawing check's alone
# ─────────────────────────────────────────────────────────────────────────────

def test_the_intra_drawing_warnings_never_change_the_headline(tmp_path):
    """The spot-check's findings are a separate list; the headline is untouched.

    The TP-104 MECH drawing has no bolts, no saddles and no skids, so the
    cross-drawing check compares nothing — while the spot-check finds a
    dozen intra-drawing overlaps.  The headline must stay
    ``insufficient_coverage``: the intra-drawing issues say something
    different, and they are not allowed to turn "we could not tell" into
    "the two drawings disagree".
    """
    from core import findings as findings_mod

    root = tmp_path / "job"
    (root / "stages").mkdir(parents=True)
    (root / "stages" / "mismatch.json").write_text(json.dumps({
        "status": status_mod.INSUFFICIENT_COVERAGE,
        "reason": "no rule had items on both sides",
        "coverage": {}, "family_coverage": {},
        "units": {"mm_per_unit": 1.0},
        "findings": [],
    }))
    (root / "stages" / "spotcheck.json").write_text(json.dumps({
        "issues": [{"type": "EQUIPMENT_OVERLAP", "severity": "WARNING",
                    "distance": 12.0, "mech_x": 1.0, "mech_y": 2.0,
                    "id": "SPOT-1", "note": "two things overlap"}],
    }))

    payload = findings_mod.build_payload(root, job_id="x")

    assert payload["status"] == status_mod.INSUFFICIENT_COVERAGE
    assert payload["status"] != status_mod.DISCREPANCIES_FOUND
    # the cross-drawing findings and the intra-drawing ones are apart
    assert payload["findings"] == []
    assert payload["other_warnings_count"] == 1
    assert len(payload["other_warnings"]) == 1
    assert payload["other_warnings"][0]["origin"] == "spotcheck"


def test_a_cross_drawing_finding_is_not_an_other_warning(tmp_path):
    """A real discrepancy stays in ``findings`` — it is not a warning."""
    from core import findings as findings_mod

    root = tmp_path / "job"
    (root / "stages").mkdir(parents=True)
    (root / "stages" / "mismatch.json").write_text(json.dumps({
        "status": status_mod.DISCREPANCIES_FOUND,
        "reason": "1 of 6 rule leg(s) compared items on both sides "
                  "and found 1 discrepancy",
        "coverage": {}, "family_coverage": {},
        "units": {"mm_per_unit": 1.0},
        "findings": [{
            "kind": "BOLT_HOLE_OFFSET", "severity": "CRITICAL",
            "origin": "mismatch", "id": "MM-0001",
            "location": [1.0, 2.0], "magnitude_mm": 30.0,
        }],
    }))
    (root / "stages" / "spotcheck.json").write_text(json.dumps({"issues": []}))

    payload = findings_mod.build_payload(root, job_id="x")

    assert payload["status"] == status_mod.DISCREPANCIES_FOUND
    assert len(payload["findings"]) == 1
    assert payload["other_warnings_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# the statuses reach the reader
# ─────────────────────────────────────────────────────────────────────────────

def _prepared_job(fx_synth_job):
    """Stages 1–7 plus fit + overlay — everything the mismatch stage needs."""
    import pipelines
    from core import fit, overlay
    from jobs import job_dir

    job_id, pair = fx_synth_job(synth.MISMATCH_HARD)
    pipelines.run_pipeline(job_id)
    d = job_dir(job_id)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": PREFIX, "axes": AXES}))
    fit.run(job_id)
    overlay.run(job_id)
    return job_id, d


def test_the_report_and_the_docx_carry_both_statuses_and_the_reason(fx_synth_job):
    """``report.json``, ``findings.json`` and the DOCX all say the same thing.

    The *pipeline's* status and the *check's* status are different things,
    and the reader has to be able to see both.
    """
    from core import findings as findings_mod
    from core import mismatch as mismatch_mod
    from core import package as package_mod
    from jobs import load_job, save_job

    job_id, d = _prepared_job(fx_synth_job)
    mismatch_mod.run(job_id)
    findings_mod.run(job_id)

    # The pipeline's own status: the job finished.  The check's status is
    # a different thing, and the report shows both.
    job = load_job(job_id)
    job.state = "done"
    save_job(job)

    package_mod.run(job_id)

    report = json.loads((d / "out" / "report.json").read_text())
    assert "status" in report
    assert report["status"]["overlay"] == "ok"
    assert report["status"]["mismatch"] == "discrepancies_found"
    assert report["status"]["reason"]
    assert report["mismatch"]["status"] == "discrepancies_found"
    assert report["mismatch"]["reason"]
    assert report["mismatch"]["family_coverage"]

    findings = json.loads((d / "out" / "findings.json").read_text())
    assert findings["status"] == "discrepancies_found"
    assert findings["reason"]
    assert findings["family_coverage"]

    # the DOCX says it too
    import docx
    doc = docx.Document(str(d / "out" / "findings.docx"))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "discrepancies_found" in text
    assert findings["reason"].split(";")[0][:40] in text


def test_the_findings_api_carries_the_status_and_the_reason(fx_client, fx_synth_job):
    """The API envelope the UI renders the banner from."""
    from core import findings as findings_mod
    from core import mismatch as mismatch_mod

    job_id, d = _prepared_job(fx_synth_job)
    mismatch_mod.run(job_id)
    findings_mod.run(job_id)

    r = fx_client.get(f"/api/v1/overlay/{job_id}/findings")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "discrepancies_found"
    assert body["reason"]
    assert body["family_coverage"]


# ─────────────────────────────────────────────────────────────────────────────
# the vocabulary itself
# ─────────────────────────────────────────────────────────────────────────────

def test_the_vocabulary_is_the_documented_one():
    assert status_mod.STATUSES == (
        "passed", "discrepancies_found", "review_required",
        "insufficient_coverage", "blocked", "failed", "disabled")
    assert status_mod.is_ok("passed")
    assert not status_mod.is_ok("insufficient_coverage")


def test_the_status_is_never_ok():
    """"ok" is not in the vocabulary — that is the whole point."""
    for status in status_mod.STATUSES:
        assert status != "ok"


def test_a_status_that_concluded_nothing_is_not_ok():
    """``insufficient_coverage`` / ``blocked`` / ``failed`` never read as ok."""
    assert not status_mod.is_ok(status_mod.INSUFFICIENT_COVERAGE)
    assert not status_mod.is_ok(status_mod.BLOCKED)
    assert not status_mod.is_ok(status_mod.FAILED)
    assert not status_mod.is_ok(status_mod.DISABLED)
    assert not status_mod.is_ok(status_mod.DISCREPANCIES_FOUND)


def test_the_decision_never_says_passed_without_a_comparison():
    """No covered rule → ``insufficient_coverage``, whatever the findings."""
    status, reason = status_mod.decide(covered_pairs=0, total_pairs=6,
                                       n_findings=0)
    assert status == status_mod.INSUFFICIENT_COVERAGE
    assert "not really compared" in reason

    status, _ = status_mod.decide(covered_pairs=0, total_pairs=6, n_findings=3)
    assert status == status_mod.INSUFFICIENT_COVERAGE


def test_only_two_statuses_are_conclusions():
    """``passed`` and ``discrepancies_found`` conclude; the rest do not."""
    assert status_mod.is_conclusive(status_mod.PASSED)
    assert status_mod.is_conclusive(status_mod.DISCREPANCIES_FOUND)
    for status in (status_mod.INSUFFICIENT_COVERAGE, status_mod.BLOCKED,
                    status_mod.FAILED, status_mod.DISABLED,
                    status_mod.REVIEW_REQUIRED):
        assert not status_mod.is_conclusive(status)
