#!/usr/bin/env python3
"""test_findings_api.py — PLAN_MISMATCH §6.4, sub-step 8b.6.

The findings API: five endpoints over the canonical ``out/findings.json``
that the merger writes.

    GET  /{job_id}/findings          the filtered list
    GET  /{job_id}/findings.csv      the CSV
    GET  /{job_id}/findings.docx     the DOCX
    GET  /{job_id}/findings/{fid}    one finding, by id or uid
    GET  /{job_id}/mismatch/policy   the resolved policy
    PUT  /{job_id}/mismatch/policy   replace it (400 on an invalid one)
    POST /{job_id}/mismatch/rerun    re-run the check (no refit)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import synth                                   # noqa: E402
from app import app as fastapi_app             # noqa: E402
from jobs import job_dir                       # noqa: E402

PREFIX = "TP104-"
AXES = ["A", "B", "C", "D", "1", "2", "3"]


@pytest.fixture
def fx_client(fx_isolated_jobs):
    return TestClient(fastapi_app)


@pytest.fixture
def fx_findings_job(fx_synth_job):
    """A finished job with findings written: (job_id, job_dir)."""
    import pipelines
    from core import fit, overlay
    from core import mismatch as mismatch_mod

    job_id, _pair = fx_synth_job(synth.MISMATCH_HARD)
    pipelines.run_pipeline(job_id)
    d = job_dir(job_id)
    (d / "axes").mkdir(exist_ok=True)
    (d / "axes" / "convention.json").write_text(json.dumps(
        {"prefix": PREFIX, "axes": AXES}))
    fit.run(job_id)
    overlay.run(job_id)
    mismatch_mod.run(job_id)
    import core.findings as findings_mod
    findings_mod.run(job_id)
    return job_id, d


# ── the list ───────────────────────────────────────────────────────────────

def test_the_findings_list(fx_client, fx_findings_job):
    """The envelope the plan specifies."""
    job_id, _d = fx_findings_job
    r = fx_client.get(f"/api/v1/overlay/{job_id}/findings")
    assert r.status_code == 200
    body = r.json()
    for key in ("job_id", "pair_id", "status", "units", "summary",
                "coverage", "warnings", "total", "returned", "items"):
        assert key in body, f"the response is missing {key}"
    assert body["status"] == "ok"
    assert body["total"] == len(body["items"])
    assert body["total"] >= 1
    # the summary is UNFILTERED
    assert body["summary"]["total"] == body["total"]


def test_the_findings_filter_by_severity(fx_client, fx_findings_job):
    job_id, _d = fx_findings_job
    r = fx_client.get(f"/api/v1/overlay/{job_id}/findings",
                      params={"severity": "CRITICAL"})
    assert r.status_code == 200
    body = r.json()
    assert body["items"]
    assert all(f["severity"] == "CRITICAL" for f in body["items"])
    # the summary still describes the whole run
    assert body["summary"]["total"] >= body["total"]


def test_the_findings_filter_by_origin_and_kind(fx_client, fx_findings_job):
    job_id, _d = fx_findings_job
    r = fx_client.get(f"/api/v1/overlay/{job_id}/findings",
                      params={"origin": "mismatch"})
    assert r.status_code == 200
    assert all(f["origin"] == "mismatch" for f in r.json()["items"])

    r = fx_client.get(f"/api/v1/overlay/{job_id}/findings",
                      params={"kind": "PATTERN_OFFSET"})
    assert r.status_code == 200
    assert all(f["kind"] == "PATTERN_OFFSET" for f in r.json()["items"])


def test_the_findings_filter_by_bbox(fx_client, fx_findings_job):
    job_id, d = fx_findings_job
    payload = json.loads((d / "out" / "findings.json").read_text())
    loc = payload["findings"][0]["location"]
    r = fx_client.get(f"/api/v1/overlay/{job_id}/findings",
                      params={"bbox": f"{loc[0]-1},{loc[1]-1},{loc[0]+1},{loc[1]+1}"})
    assert r.status_code == 200
    assert r.json()["total"] >= 1


def test_a_bad_filter_is_a_400(fx_client, fx_findings_job):
    """Bad severity / kind / origin / bbox → 400, not 500."""
    job_id, _d = fx_findings_job
    for params in ({"severity": "NOPE"},
                   {"kind": "NOT_A_KIND"},
                   {"origin": "nowhere"},
                   {"bbox": "1,2,3"},
                   {"bbox": "a,b,c,d"},
                   {"limit": 0},
                   {"limit": 99999},
                   {"offset": -1}):
        r = fx_client.get(f"/api/v1/overlay/{job_id}/findings", params=params)
        assert r.status_code == 400, f"{params} gave {r.status_code}"
        assert r.json()["detail"], "the 400 must explain itself"


def test_findings_for_an_unknown_job_is_a_404(fx_client):
    r = fx_client.get("/api/v1/overlay/nosuchjob/findings")
    assert r.status_code == 404


def test_findings_before_the_stage_is_a_404(fx_client, fx_synth_job):
    """No findings yet → 404, not an empty list."""
    job_id, _pair = fx_synth_job(synth.HARD)
    r = fx_client.get(f"/api/v1/overlay/{job_id}/findings")
    assert r.status_code == 404


# ── one finding ────────────────────────────────────────────────────────────

def test_one_finding_by_id_and_by_uid(fx_client, fx_findings_job):
    job_id, d = fx_findings_job
    payload = json.loads((d / "out" / "findings.json").read_text())
    first = payload["findings"][0]

    r = fx_client.get(f"/api/v1/overlay/{job_id}/findings/{first['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == first["id"]

    r = fx_client.get(f"/api/v1/overlay/{job_id}/findings/{first['uid']}")
    assert r.status_code == 200
    assert r.json()["uid"] == first["uid"]


def test_an_unknown_finding_is_a_404(fx_client, fx_findings_job):
    job_id, _d = fx_findings_job
    r = fx_client.get(f"/api/v1/overlay/{job_id}/findings/NOPE-999")
    assert r.status_code == 404


# ── the file endpoints ─────────────────────────────────────────────────────

def test_the_findings_csv(fx_client, fx_findings_job):
    job_id, _d = fx_findings_job
    r = fx_client.get(f"/api/v1/overlay/{job_id}/findings.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    text = r.text
    assert "id,uid,kind,severity" in text.splitlines()[0]


def test_the_findings_docx(fx_client, fx_findings_job):
    job_id, _d = fx_findings_job
    r = fx_client.get(f"/api/v1/overlay/{job_id}/findings.docx")
    assert r.status_code == 200
    assert "wordprocessingml" in r.headers["content-type"]
    assert len(r.content) > 1000


def test_the_findings_files_before_the_stage_are_404(fx_client, fx_synth_job):
    job_id, _pair = fx_synth_job(synth.HARD)
    assert fx_client.get(
        f"/api/v1/overlay/{job_id}/findings.csv").status_code == 404
    assert fx_client.get(
        f"/api/v1/overlay/{job_id}/findings.docx").status_code == 404


# ── the policy ─────────────────────────────────────────────────────────────

def test_the_mismatch_policy_can_be_read(fx_client, fx_findings_job):
    job_id, _d = fx_findings_job
    r = fx_client.get(f"/api/v1/overlay/{job_id}/mismatch/policy")
    assert r.status_code == 200
    body = r.json()
    assert body["policy_hash"]
    assert body["policy"]["enabled"] is True


def test_the_mismatch_policy_can_be_replaced(fx_client, fx_findings_job):
    job_id, d = fx_findings_job
    r = fx_client.put(f"/api/v1/overlay/{job_id}/mismatch/policy",
                      json={"enabled": False, "max_findings": 100})
    assert r.status_code == 200
    assert r.json()["policy"]["enabled"] is False
    assert (job_dir(job_id) / "mismatch_policy.json").exists()


def test_an_invalid_mismatch_policy_is_a_400(fx_client, fx_findings_job):
    job_id, _d = fx_findings_job
    r = fx_client.put(f"/api/v1/overlay/{job_id}/mismatch/policy",
                      json={"nonsense_key": 1})
    assert r.status_code == 400
    assert "invalid" in r.json()["detail"].lower()


# ── the rerun ──────────────────────────────────────────────────────────────

def test_the_mismatch_can_be_re_run(fx_client, fx_findings_job):
    """The rerun re-runs the check without a refit."""
    job_id, d = fx_findings_job
    before = json.loads((d / "stages" / "mismatch.json").read_text())

    r = fx_client.post(f"/api/v1/overlay/{job_id}/mismatch/rerun")
    assert r.status_code == 200
    assert r.json()["status"] == "rerun"

    after = json.loads((d / "stages" / "mismatch.json").read_text())
    assert after["status"] == "ok"
    assert len(after["findings"]) == len(before["findings"])


def test_the_rerun_rejects_a_traversing_pair_id(fx_client, fx_findings_job):
    job_id, _d = fx_findings_job
    r = fx_client.post(f"/api/v1/overlay/{job_id}/mismatch/rerun",
                       params={"pair_id": "../../.."})
    assert r.status_code == 400
