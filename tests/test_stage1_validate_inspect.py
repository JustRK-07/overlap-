#!/usr/bin/env python3
"""test_stage1_validate_inspect.py — stages 1 and 2 (validate / inspect).

Both stages report the drawing's extents.  They used to call
``msp.bbox()``, which does not exist in ezdxf 1.4.4 — the ``AttributeError``
was swallowed by a blanket ``except``, so every drawing reported
``"extents": null`` and nothing said so.

A5.
"""
from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest

from core import inspect as inspect_mod
from core import validate as validate_mod
from jobs import job_dir


def _dxf_with_geometry(path: Path) -> None:
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_line((10.0, 20.0), (110.0, 20.0), dxfattribs={"layer": "A"})
    msp.add_line((10.0, 20.0), (10.0, 70.0), dxfattribs={"layer": "A"})
    msp.add_circle((60.0, 45.0), 5.0, dxfattribs={"layer": "B"})
    doc.saveas(str(path))


def test_validate_reports_the_extents(fx_isolated_jobs):
    """A5: a readable DXF must report a real extent, not null."""
    job_id = "extents-validate"
    d = job_dir(job_id)
    (d / "uploads").mkdir(parents=True, exist_ok=True)
    (d / "stages").mkdir(parents=True, exist_ok=True)
    _dxf_with_geometry(d / "uploads" / "a.dxf")

    validate_mod.run(job_id)

    info = json.loads((d / "stages" / "validate.json").read_text())
    entry = info["a.dxf"]
    assert entry["readable"] is True
    assert entry["extents"] is not None, (
        "extents must be reported — msp.bbox() does not exist in ezdxf 1.4.4")
    assert entry["extents"]["min"] == pytest.approx([10.0, 20.0], abs=1e-6)
    assert entry["extents"]["max"] == pytest.approx([110.0, 70.0], abs=1e-6)


def test_inspect_reports_the_extents(fx_isolated_jobs):
    """A5: the same for the inspect stage."""
    job_id = "extents-inspect"
    d = job_dir(job_id)
    (d / "uploads").mkdir(parents=True, exist_ok=True)
    (d / "stages").mkdir(parents=True, exist_ok=True)
    _dxf_with_geometry(d / "uploads" / "a.dxf")

    inspect_mod.run(job_id)

    info = json.loads((d / "stages" / "inspect.json").read_text())
    entry = info["a.dxf"]
    assert entry["extents"] is not None, (
        "extents must be reported — msp.bbox() does not exist in ezdxf 1.4.4")
    assert entry["extents"]["min"] == pytest.approx([10.0, 20.0], abs=1e-6)
    assert entry["extents"]["max"] == pytest.approx([110.0, 70.0], abs=1e-6)


def test_extents_are_null_for_an_empty_drawing(fx_isolated_jobs):
    """An empty modelspace has no extent — it must report null, not a zero box."""
    job_id = "extents-empty"
    d = job_dir(job_id)
    (d / "uploads").mkdir(parents=True, exist_ok=True)
    (d / "stages").mkdir(parents=True, exist_ok=True)
    ezdxf.new("R2018").saveas(str(d / "uploads" / "empty.dxf"))

    validate_mod.run(job_id)
    info = json.loads((d / "stages" / "validate.json").read_text())
    assert info["empty.dxf"]["extents"] is None
