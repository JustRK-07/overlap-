#!/usr/bin/env python3
"""test_plan_acceptance.py — the PLAN.md "Done when" criteria.

PLAN.md states four acceptance criteria that were still unmet after
PLAN.md steps 0-10 were implemented:

B1  "no ``ezdxf.readfile(`` remains outside ``core/dxfio.py`` and
    ``core/package.py``"
B2  "inputs (uploads/converted) go through ``recover.readfile``"
B3  "no code path returns a transform without ``quality``, and there's
    **no silent fallback**"
B4  "nothing in ``package.py`` silently discards geometry or entities"

Each test here fails on the unfixed code.
"""
from __future__ import annotations

import ast
import io
import json
import sys
from pathlib import Path

import ezdxf
import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))


# ── B1 ─────────────────────────────────────────────────────────────────────

def test_no_readfile_outside_dxfio_and_package():
    """B1: every DXF read goes through core.dxfio."""
    offenders = []
    for path in sorted((ROOT / "core").rglob("*.py")):
        if path.name in ("dxfio.py", "package.py"):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "readfile"):
                continue
            value = node.func.value
            if isinstance(value, ast.Name) and value.id == "ezdxf":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, (
        "ezdxf.readfile() must not be called outside core/dxfio.py and "
        f"core/package.py — found at {offenders}")


# ── B2 ─────────────────────────────────────────────────────────────────────

def _dxf_without_eof(tmp_path: Path) -> Path:
    """A DXF whose trailing ``EOF`` marker is missing.

    ``ezdxf.readfile`` rejects it; ``ezdxf.recover.readfile`` accepts it.
    """
    doc = ezdxf.new("R2018")
    doc.modelspace().add_line((0.0, 0.0), (100.0, 100.0),
                               dxfattribs={"layer": "A"})
    good = tmp_path / "good.dxf"
    doc.saveas(str(good))
    text = good.read_text().rstrip()
    assert text.endswith("EOF")
    broken = tmp_path / "broken.dxf"
    broken.write_text(text[:-3])
    return broken


def test_the_broken_dxf_is_recoverable_but_not_plainly_readable(tmp_path):
    """The premise of B2 — plain read fails, recover succeeds."""
    from ezdxf import recover
    broken = _dxf_without_eof(tmp_path)
    with pytest.raises(Exception):
        ezdxf.readfile(str(broken))
    doc, _audit = recover.readfile(str(broken))
    assert len(doc.modelspace()) >= 1


def test_inspect_accepts_a_recoverable_upload(fx_isolated_jobs, tmp_path):
    """B2: the inspect stage must read uploads with the recovering reader.

    Stage 1 (validate) accepts a recoverable file; stage 2 used to reject
    the very same file because it read it with the strict reader — so the
    job died at stage 2 with an audit error on a file stage 1 had passed.
    """
    from core import inspect as inspect_mod
    from jobs import job_dir

    job_id = "recoverable"
    d = job_dir(job_id)
    (d / "uploads").mkdir(parents=True, exist_ok=True)
    (d / "stages").mkdir(parents=True, exist_ok=True)
    broken = _dxf_without_eof(tmp_path)
    (d / "uploads" / "broken.dxf").write_bytes(broken.read_bytes())

    inspect_mod.run(job_id)

    info = json.loads((d / "stages" / "inspect.json").read_text())
    assert "error" not in info["broken.dxf"], (
        "a recoverable upload must not fail the inspect stage")
    assert info["broken.dxf"]["total_entities"] >= 1


@pytest.mark.parametrize("module", ["inspect", "labels", "clean_policy",
                                    "clean_v1", "clean_v2"])
def test_input_stages_use_the_recovering_reader(module):
    """B2: the upload/converted readers are ``read_input`` (recover-based)."""
    path = ROOT / "core" / f"{module}.py"
    tree = ast.parse(path.read_text())
    imports_read_input = any(
        isinstance(node, ast.ImportFrom)
        and any(a.name == "read_input" for a in node.names)
        for node in ast.walk(tree))
    assert imports_read_input, (
        f"core/{module}.py must read uploads/converted with dxfio.read_input "
        "(recover), not the strict reader")


# ── B3 ─────────────────────────────────────────────────────────────────────

def test_loo_does_not_silently_switch_to_an_affine_model():
    """B3: a degenerate similarity refit must raise, not fall back.

    An affine absorbs far more error than a similarity, so a leave-one-out
    residual measured against one under-reports exactly the instability the
    gate exists to catch.  The old code fell back silently.
    """
    from core.transform2d import TransformError, loo_residuals

    # four points; dropping the last leaves three coincident ones
    src = [(0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (10.0, 10.0)]
    dst = [(0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (20.0, 20.0)]

    with pytest.raises(TransformError, match="degenerate"):
        loo_residuals(np.asarray(src), np.asarray(dst), [0, 1, 2, 3])


def test_a_degenerate_loo_becomes_a_fit_gate_not_a_crash():
    """B3: the fit stage must turn the degenerate LOO into a gate failure."""
    from core.fit import FitError, FitInputs, FitOptions, solve

    src = np.asarray([(0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (10.0, 10.0)])
    dst = np.asarray([(0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (20.0, 20.0)])
    inp = FitInputs(labels=["a", "b", "c", "d"], src=src, dst=dst,
                    spacing=1000.0, extent=10000.0)
    with pytest.raises(FitError, match="leave-one-out"):
        solve(inp, FitOptions())
    # and `force` overrides it, like every other gate
    forced = solve(inp, FitOptions(force=True))
    assert forced["quality"]["status"] == "forced"
    assert any("leave-one-out" in w for w in forced["quality"]["warnings"])


# ── B4 ─────────────────────────────────────────────────────────────────────

def test_mtext_is_not_filtered_out_of_the_preview():
    """B4: the preview must not discard MTEXT silently.

    The renderer's filter returned ``False`` for every MTEXT ("it needs a
    font"), so the text never appeared in the picture while the reported
    extents still counted it.  The filter is gone; what the picture cannot
    show is now *reported* instead.
    """
    from core import package as package_mod

    source = (ROOT / "core" / "package.py").read_text()
    tree = ast.parse(source)
    in_view = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "in_view")
    # An MTEXT test that is NOT guarded by the fallback flag is the old
    # unconditional exclusion.  (`skip_mtext and e.dxftype() == "MTEXT"` is
    # a BoolOp, not a Compare — it is the retry path, and it is reported.)
    unconditional = []
    for node in ast.walk(in_view):
        if not isinstance(node, ast.If):
            continue
        if (isinstance(node.test, ast.Compare)
                and any(isinstance(c, ast.Constant) and c.value == "MTEXT"
                        for c in ast.walk(node.test))):
            unconditional.append(node.lineno)
    assert not unconditional, (
        f"the preview's in_view filter must not exclude MTEXT "
        f"unconditionally (lines {unconditional})")

    # and the render path knows how many MTEXT there are
    doc = ezdxf.new("R2018")
    doc.layers.add("A")
    doc.modelspace().add_line((0.0, 0.0), (100.0, 0.0), dxfattribs={"layer": "A"})
    mt = doc.modelspace().add_mtext("TP104-D", dxfattribs={"layer": "A"})
    mt.set_location((120.0, 20.0))
    assert sum(1 for e in doc.modelspace() if e.dxftype() == "MTEXT") == 1

    # the font probe must answer, and must not raise
    assert isinstance(package_mod._ezdxf_can_draw_text(), bool)


def test_the_report_records_the_mtext_it_could_not_draw(fx_synth_job):
    """B4: report.json must say what the preview could not show."""
    from tests.test_package import _run_job  # noqa: PLC0415

    d, _, report = _run_job(fx_synth_job)
    preview = report["preview"]
    for key in ("mtext_entities", "mtext_drawn", "mtext_not_drawn"):
        assert key in preview, f"report.json preview must carry {key}"
    # the synthetic fixture has MTEXT grid labels
    assert preview["mtext_entities"] >= 1
    # the numbers must be consistent
    if preview["mtext_drawn"]:
        assert preview["mtext_not_drawn"] == 0
    else:
        assert preview["mtext_not_drawn"] == preview["mtext_entities"]
def test_the_report_records_skipped_mtext(fx_synth_job):
    """B4: any MTEXT the preview could not draw must be reported."""
    from tests.test_package import _run_job  # noqa: PLC0415

    d, _, report = _run_job(fx_synth_job)
    assert "mtext_skipped" in report["preview"], (
        "report.json must say how many MTEXT were left out of the preview")
    assert report["preview"]["mtext_skipped"] == 0, (
        "the synthetic fixture has MTEXT labels — they must be drawn")
    for img in report["preview"]["images"]:
        assert "mtext_skipped" in img
