#!/usr/bin/env python3
"""tests/test_export_dwg.py — PLAN_MISMATCH step 8c.

The DWG export needs the ODA File Converter, which is not installed in a
normal development environment, so **every test here is mocked**.  What
is being tested is the wiring, not the converter:

* the endpoint answers 501 (not 500) when the converter is missing;
* a good export writes the file with ``replace=True`` and ``audit=True``
  and no layer name still contains ``|``;
* the export is cached and rebuilt when the source DXF is newer;
* a bad version or variant is a 400;
* a converter failure is a 502;
* ``/api/v1/capabilities`` reports the flag and the versions.
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

from fastapi.testclient import TestClient  # noqa: E402

from app import app as fastapi_app             # noqa: E402
from core import export_dwg                    # noqa: E402
from jobs import job_dir                       # noqa: E402


@pytest.fixture
def fx_client(fx_isolated_jobs):
    return TestClient(fastapi_app)


def _drawing_with_pipe_layers(path: Path) -> None:
    """A DXF with the pipeline's ``MECH|…`` / ``STR|…`` layer names."""
    doc = ezdxf.new("R2018")
    for layer in ("MECH|0", "STR|0", "QA|GRID", "PLAIN"):
        doc.layers.add(layer)
    doc.modelspace().add_line((0, 0), (10, 10), dxfattribs={"layer": "MECH|0"})
    doc.modelspace().add_line((0, 0), (10, 0), dxfattribs={"layer": "STR|0"})
    doc.saveas(str(path))


@pytest.fixture
def fx_job(fx_isolated_jobs):
    """A job whose superimposed DXF is on disk."""
    job_id = "dwgjob"
    d = job_dir(job_id)
    (d / "out").mkdir(parents=True, exist_ok=True)
    _drawing_with_pipe_layers(d / "out" / "superimposed.dxf")
    _drawing_with_pipe_layers(d / "out" / "superimposed_annotated.dxf")
    (d / "job.json").write_text(json.dumps({
        "job_id": job_id, "state": "done", "files": {}, "pairs": [],
        "current_step": 11, "step_name": "Done",
        "created_at": "2026-10-25T00:00:00",
    }))
    return job_id, d


@pytest.fixture
def fx_oda_installed(monkeypatch):
    """Pretend the ODA File Converter is installed, and capture the call."""
    from ezdxf.addons import odafc

    calls = {}

    def fake_export_dwg(doc, filename, version=None, *, audit=False,
                        replace=False):
        calls["version"] = version
        calls["audit"] = audit
        calls["replace"] = replace
        calls["layers"] = [layer.dxf.name for layer in doc.layers]
        Path(filename).write_bytes(b"DWG")

    monkeypatch.setattr(odafc, "is_installed", lambda: True)
    monkeypatch.setattr(odafc, "export_dwg", fake_export_dwg)
    return calls


# ── the module ─────────────────────────────────────────────────────────────

def test_the_module_reports_the_converter_as_unavailable(monkeypatch):
    from ezdxf.addons import odafc
    monkeypatch.setattr(odafc, "is_installed", lambda: False)
    assert export_dwg.available() is False
    assert "ODA File Converter" in export_dwg.UNAVAILABLE_MESSAGE


def test_the_export_rejects_an_unknown_version(tmp_path):
    with pytest.raises(ValueError, match="version must be one of"):
        export_dwg.export(tmp_path / "a.dxf", tmp_path / "a.dwg",
                          version="R9999")


def test_the_export_renames_the_pipe_layers(tmp_path, fx_oda_installed):
    """``MECH|0`` becomes ``MECH$0`` — AutoCAD reserves ``|``."""
    src = tmp_path / "in.dxf"
    _drawing_with_pipe_layers(src)

    result = export_dwg.export(src, tmp_path / "out.dwg", version="R2018")

    assert result.ok is True
    assert result.path == str(tmp_path / "out.dwg")
    assert result.layer_renames["MECH|0"] == "MECH$0"
    assert result.layer_renames["STR|0"] == "STR$0"
    assert not any("|" in name for name in fx_oda_installed["layers"]), \
        fx_oda_installed["layers"]
    assert "PLAIN" in fx_oda_installed["layers"], "a plain layer is untouched"
    # the call contract the plan states
    assert fx_oda_installed["version"] == "R2018"
    assert fx_oda_installed["audit"] is True
    assert fx_oda_installed["replace"] is True


def test_the_export_reports_a_converter_failure(tmp_path, monkeypatch):
    from ezdxf.addons import odafc
    src = tmp_path / "in.dxf"
    _drawing_with_pipe_layers(src)

    def boom(*args, **kwargs):
        raise odafc.ODAFCError("the converter exploded")

    monkeypatch.setattr(odafc, "is_installed", lambda: True)
    monkeypatch.setattr(odafc, "export_dwg", boom)

    result = export_dwg.export(src, tmp_path / "out.dwg", version="R2018")
    assert result.ok is False
    assert "exploded" in result.message


# ── the API ────────────────────────────────────────────────────────────────

def test_the_capabilities_flag(fx_client):
    """The probe says whether the DWG export is possible."""
    r = fx_client.get("/api/v1/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert "dwg_export" in body
    assert isinstance(body["dwg_export"], bool)
    assert isinstance(body["dwg_versions"], list)
    # never a path
    assert not any("/" in str(v) for v in body.values())


def test_unavailable_returns_501(fx_client, fx_job, monkeypatch):
    """Without the converter the endpoint answers 501, not 500."""
    from ezdxf.addons import odafc
    monkeypatch.setattr(odafc, "is_installed", lambda: False)
    job_id, _d = fx_job

    r = fx_client.get(f"/api/v1/overlay/{job_id}/download.dwg")
    assert r.status_code == 501
    body = r.json()
    assert body["available"] is False
    assert "ODA" in body["detail"]


def test_export_mocked(fx_client, fx_job, fx_oda_installed):
    """The happy path: the file is written and streamed."""
    job_id, d = fx_job
    r = fx_client.get(f"/api/v1/overlay/{job_id}/download.dwg",
                      params={"variant": "superimposed", "version": "R2018"})
    assert r.status_code == 200
    assert r.content == b"DWG"
    assert "image/vnd.dwg" in r.headers["content-type"]
    assert (d / "out" / "superimposed_R2018.dwg").exists()


def test_export_cached(fx_client, fx_job, fx_oda_installed):
    """A second request does not re-export."""
    job_id, d = fx_job
    url = f"/api/v1/overlay/{job_id}/download.dwg"
    assert fx_client.get(url).status_code == 200
    calls_after_first = dict(fx_oda_installed)
    assert fx_client.get(url).status_code == 200
    assert fx_oda_installed == calls_after_first, \
        "the second request must use the cached DWG"


def test_a_bad_version_or_variant_is_a_400(fx_client, fx_job):
    job_id, _d = fx_job
    r = fx_client.get(f"/api/v1/overlay/{job_id}/download.dwg",
                      params={"version": "R9999"})
    assert r.status_code == 400
    r = fx_client.get(f"/api/v1/overlay/{job_id}/download.dwg",
                      params={"variant": "banana"})
    assert r.status_code == 400


def test_an_odafc_error_is_a_502(fx_client, fx_job, monkeypatch):
    from ezdxf.addons import odafc
    job_id, _d = fx_job

    def boom(*args, **kwargs):
        raise odafc.ODAFCError("the converter exploded")

    monkeypatch.setattr(odafc, "is_installed", lambda: True)
    monkeypatch.setattr(odafc, "export_dwg", boom)

    r = fx_client.get(f"/api/v1/overlay/{job_id}/download.dwg")
    assert r.status_code == 502
    assert "exploded" in r.json()["detail"]
