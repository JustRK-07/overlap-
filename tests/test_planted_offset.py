#!/usr/bin/env python3
"""tests/test_planted_offset.py — a planted defect the engine cannot see yet.

The scene: a 200 × 150 mm rectangle on layer ``NEUTRAL`` is added to **both**
drawings of a synthetic MECH/STR pair.  The STR copy is displaced by
**+7.5 mm in X**.  The pair is then put through the whole pipeline and the
test asserts that exactly one finding reports a magnitude of 7.5 mm.

Today it does not: ``NEUTRAL`` matches none of the mismatch engine's
families, so there is no offset engine and nothing reports the planted
displacement.  The test is therefore marked::

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="offset engine not implemented",
    )

``strict=True`` is the point of the marker.  The moment the engine lands and
the assertion holds, pytest reports **XPASS(strict)** — a *failure* — which
forces whoever implemented it to come back and remove the marker.  A test that
merely skipped would have gone quietly green and stayed that way.

``raises=AssertionError`` keeps the marker honest in the other direction: only
the target assertion is allowed to xfail.  A broken fixture, a crashed
pipeline or a missing artefact raises :class:`PlantError` instead, and is
reported as a real failure rather than being swallowed by the xfail.

The plant itself is verified independently of the engine: the test checks
that the fitted transform really does put the two rectangles 7.5 mm apart in
MECH world, so an xfail can only ever mean *"the engine is missing"* and
never *"we planted nothing"*.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import ezdxf
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import synth                                   # noqa: E402

# ── the scene ──────────────────────────────────────────────────────────────

#: The planted rectangle, in millimetres.
RECT_W = 200.0
RECT_H = 150.0

#: How far the STR copy is displaced, in millimetres, along +X.
OFFSET_MM = 7.5

#: The layer both copies live on.  Deliberately *not* one of the mismatch
#: engine's families — that is the whole point of the test.
LAYER = "NEUTRAL"

#: Where the rectangle sits, in the synthetic drawings' shared frame P.
#: Chosen inside the grid, clear of the axis lines and of the STR columns
#: (which the generator plants on every grid intersection).
ANCHOR_P = (12000.0, 3000.0)

#: The grid convention the synthetic pair uses.
PREFIX = "TP104-"
AXES = ["A", "B", "C", "D", "1", "2", "3"]

#: How closely the fitted transform must reproduce the planted displacement.
FIT_TOLERANCE_MM = 1e-3


class PlantError(RuntimeError):
    """The fixture is broken — not the engine's fault.

    Raised instead of ``AssertionError`` so that ``xfail(raises=AssertionError)``
    cannot hide it: a broken plant must fail loudly, not xfail quietly.
    """


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PlantError(message)


# ── helpers ────────────────────────────────────────────────────────────────

def _plant_rect(path: Path, cx: float, cy: float, shift_x: float = 0.0) -> None:
    """Add the ``NEUTRAL`` rectangle to the DXF at ``path``."""
    doc = ezdxf.readfile(str(path))
    if LAYER not in doc.layers:
        doc.layers.add(LAYER)
    x0 = cx + shift_x
    y0 = cy
    doc.modelspace().add_lwpolyline(
        [(x0, y0), (x0 + RECT_W, y0), (x0 + RECT_W, y0 + RECT_H), (x0, y0 + RECT_H)],
        close=True,
        dxfattribs={"layer": LAYER},
    )
    doc.saveas(str(path))


def _run_pipeline(job_id: str, job_path: Path) -> None:
    """Stages 1–6, the grid convention, then stages 8–11."""
    import pipelines

    pipelines.run_pipeline(job_id)

    (job_path / "axes").mkdir(exist_ok=True)
    (job_path / "axes" / "convention.json").write_text(
        json.dumps({"prefix": PREFIX, "axes": AXES}))

    pipelines.resume_after_axes(job_id)


def _apply(transform: dict, x: float, y: float) -> tuple[float, float]:
    """Map a STR-file point into MECH world with the fitted transform."""
    a = float(transform["a"])
    b = float(transform["b"])
    c = float(transform["c"])
    d = float(transform["d"])
    tx = float(transform["tx"])
    ty = float(transform["ty"])
    return (a * x + b * y + tx, c * x + d * y + ty)


def _magnitude(finding: dict) -> float | None:
    """The finding's magnitude, the way ``core.mismatch.model.Finding`` defines it.

    ``magnitude_mm`` when it is set, otherwise the Euclidean length of
    ``offset_mm``.  Mirroring the model rather than reading one field keeps
    the test from getting stuck xfailing over which of the two an
    implementation happens to populate.
    """
    magnitude = finding.get("magnitude_mm")
    if magnitude is not None:
        return float(magnitude)
    offset = finding.get("offset_mm")
    if not offset:
        return None
    return float((offset[0] ** 2 + offset[1] ** 2) ** 0.5)


# ── the test ───────────────────────────────────────────────────────────────

def test_the_planted_offset_is_reported(fx_isolated_jobs, tmp_path):
    """A rectangle displaced by 7.5 mm must be reported with that magnitude.

    The rectangle is planted on layer ``NEUTRAL`` in both drawings — a
    layer no family claims — so the only thing that can find it is the
    offset check's pairing rule.
    """
    from jobs import create_job_legacy, job_dir

    # ── the scene ──────────────────────────────────────────────────────────
    pair = synth.build_pair(tmp_path / "planted_src", synth.LEGACY_COMPATIBLE)
    mech_origin = (ANCHOR_P[0] + pair.opts.mech_offset[0],
                   ANCHOR_P[1] + pair.opts.mech_offset[1])
    str_origin = (ANCHOR_P[0] + pair.opts.str_offset[0],
                  ANCHOR_P[1] + pair.opts.str_offset[1])
    _plant_rect(pair.mech_path, *mech_origin)
    _plant_rect(pair.str_path, *str_origin, shift_x=OFFSET_MM)

    # ── the pipeline ───────────────────────────────────────────────────────
    job = create_job_legacy(
        drawing_a_name=pair.mech_path.name,
        drawing_a_size=pair.mech_path.stat().st_size,
        drawing_b_name=pair.str_path.name,
        drawing_b_size=pair.str_path.stat().st_size,
        clean_version="v1",
    )
    job_path = job_dir(job.job_id)
    uploads = job_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pair.mech_path, uploads / pair.mech_path.name)
    shutil.copyfile(pair.str_path, uploads / pair.str_path.name)

    _run_pipeline(job.job_id, job_path)

    # ── the plant is real, whatever the engine does ────────────────────────
    transform_path = job_path / "transform.json"
    _require(transform_path.exists(),
             f"the fit wrote no transform.json under {job_path}")
    transform = json.loads(transform_path.read_text())

    mapped = _apply(transform, str_origin[0] + OFFSET_MM, str_origin[1])
    dx = mapped[0] - mech_origin[0]
    dy = mapped[1] - mech_origin[1]
    _require(abs(dx - OFFSET_MM) <= FIT_TOLERANCE_MM and abs(dy) <= FIT_TOLERANCE_MM,
             f"the planted displacement is not {OFFSET_MM} mm: "
             f"got ({dx:.6f}, {dy:.6f}) mm after the fit")

    # ── and the engine must report it ─────────────────────────────────────
    findings_path = job_path / "out" / "findings.json"
    _require(findings_path.exists(),
             f"the pipeline wrote no out/findings.json under {job_path}")
    payload = json.loads(findings_path.read_text())
    from core.mismatch import status as status_mod
    _require(payload.get("status") in status_mod.STATUSES,
             f"the findings report is not usable: status={payload.get('status')!r}")

    reported = [f for f in payload.get("findings", [])
                if _magnitude(f) is not None
                and abs(_magnitude(f) - OFFSET_MM) <= FIT_TOLERANCE_MM]

    assert len(reported) == 1, (
        f"expected exactly one finding with magnitude {OFFSET_MM} mm, "
        f"found {len(reported)}; "
        f"the report has {len(payload.get('findings', []))} finding(s) in total: "
        + ", ".join(f"{f.get('kind')}={_magnitude(f)}"
                    for f in payload.get("findings", []))
    )
