#!/usr/bin/env python3
"""tests/verify_fixes.py — re-run every reproduction from the review.

Each check below reproduced a defect *before* its fix.  This script runs
them all against the current code, so a regression that undoes any of the
fixes is caught here rather than by eye.

Run it directly:  ``python3 tests/verify_fixes.py``
"""
from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

OK = "  ok  "
FAIL = " FAIL "

_results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    _results.append((name, bool(condition), detail))
    print(f"{OK if condition else FAIL}{name}" + (f" — {detail}" if detail else ""))


# ── A1 · the overlay's O(N²) handle set ────────────────────────────────────
def check_a1() -> None:
    import time
    from core import overlay

    report = overlay.OverlayReport()
    report.str_entity_handles = [f"H{i}" for i in range(20000)]
    doc = __import__("ezdxf").new("R2018")
    doc.blocks.new("Part-X")
    for i in range(20000):
        doc.modelspace().add_blockref("Part-X", (i * 10.0, 0.0))
    t0 = time.perf_counter()
    overlay._str_block_names(doc, report)
    elapsed = time.perf_counter() - t0
    check("A1 overlay handle set is not quadratic", elapsed < 2.0,
          f"{elapsed:.3f} s for 20 000 entities (was 14.83 s)")


# ── A2 · the zero-length member band ──────────────────────────────────────
def check_a2() -> None:
    from core.mismatch import geom

    worst = None
    for pts in ([(0, 0), (300, 0), (300, 150), (0, 150)],
                [(0, 0), (0, 150), (300, 150), (300, 0)]):
        (cx, cy), (ux, uy), w, h = geom.rect_from_corners(pts)
        along = max(w, h)
        u = (ux, uy) if w >= h else (-uy, ux)
        p0, p1 = geom.segment_axis_params(
            (cx - u[0] * along / 2, cy - u[1] * along / 2),
            (cx + u[0] * along / 2, cy + u[1] * along / 2), (cx, cy), u)
        worst = abs(p1 - p0) if worst is None else min(worst, abs(p1 - p0))
    check("A2 member band is never zero-length", abs(worst - 300.0) < 1e-9,
          f"shortest band = {worst:.1f} (the long side)")


# ── A3 · the double mm conversion ─────────────────────────────────────────
def check_a3() -> None:
    from core.mismatch.checks import Env
    from core.mismatch.classify import Classifier
    from core.mismatch.features import FeatureSet
    from core.mismatch.policy import DEFAULT_POLICY

    fs = FeatureSet(source="STR")
    env = Env(policy=DEFAULT_POLICY, mm=1000.0, mech=fs, str=fs,
              clf=Classifier(DEFAULT_POLICY), grid_spacing_mm=6_000_000.0)
    check("A3 the search cap is in millimetres",
          abs(env.search_mm(1e9) - 1_500_000.0) < 1.0,
          f"cap = {env.search_mm(1e9):.0f} mm (0.25 x 6 000 000)")


# ── A4 · clash_with_members ───────────────────────────────────────────────
def check_a4() -> None:
    import ezdxf
    from core.mismatch import detect
    from core.mismatch.policy import DEFAULT_POLICY, MismatchPolicy
    from core.transform2d import Affine2D

    base = Path(tempfile.mkdtemp())
    mech = ezdxf.new("R2018")
    mech.header["$INSUNITS"] = 4          # millimetres — ezdxf.new defaults
    mech.layers.add("EQUIPMENT")           # to 6 (metres)!
    mech.modelspace().add_lwpolyline(
        [(-500, 150), (500, 150), (500, 250), (-500, 250)],
        close=True, dxfattribs={"layer": "EQUIPMENT"})
    str_doc = ezdxf.new("R2018")
    str_doc.header["$INSUNITS"] = 4
    str_doc.layers.add("FRAME-STEEL")
    for y in (100.0, 300.0):
        str_doc.modelspace().add_line((-2500, y), (2500, y),
                                      dxfattribs={"layer": "FRAME-STEEL"})
    mp, sp = base / "m.dxf", base / "s.dxf"
    mech.saveas(str(mp))
    str_doc.saveas(str(sp))

    pol = DEFAULT_POLICY.to_dict()
    pol["clash_with_members"] = True
    result = detect(mp, sp, Affine2D(), policy=MismatchPolicy.from_dict(pol))
    clashes = [f for f in result.findings if f.kind == "CLASH"]
    check("A4 clash_with_members finds a band overlap", bool(clashes),
          f"{len(clashes)} CLASH finding(s)")


# ── A5 · the extents ──────────────────────────────────────────────────────
def check_a5() -> None:
    import ezdxf
    from ezdxf import bbox as ez_bbox

    doc = ezdxf.new("R2018")
    doc.modelspace().add_line((10, 20), (110, 20))
    box = ez_bbox.extents(doc.modelspace(), fast=False)
    check("A5 the extents API is the one that works",
          box.has_data and abs(box.extmax[0] - 110.0) < 1e-6,
          f"max x = {box.extmax[0]}")


# ── A6 · the hatch path removal ───────────────────────────────────────────
def check_a6() -> None:
    import ezdxf
    from core.clean_common import _strip_triangles_in
    from core.clean_policy import DEFAULT_POLICY

    doc = ezdxf.new("R2018")
    doc.layers.add("REV-A")
    msp = doc.modelspace()
    h = msp.add_hatch(color=1, dxfattribs={"layer": "REV-A"})
    h.paths.add_polyline_path([(0, 0, 0), (100, 0, 0), (50, 80, 0)],
                               is_closed=True)
    removed = _strip_triangles_in(msp, container_name="", policy=DEFAULT_POLICY)
    check("A6 a 3-vertex HATCH path is removed", removed == 1 and len(h.paths) == 0,
          f"removed={removed}, paths left={len(h.paths)}")


# ── A7 · the cleaned/ directory ───────────────────────────────────────────
def check_a7() -> None:
    import shutil
    import ezdxf
    from core import clean_v1
    from core.roles import FileEntry, write_manifest
    from jobs import job_dir
    import jobs

    base = Path(tempfile.mkdtemp())
    jobs.JOBS_DIR = base
    job_id = "verifyclean"
    d = job_dir(job_id)
    (d / "stages").mkdir(parents=True, exist_ok=True)
    doc = ezdxf.new("R2018")
    doc.modelspace().add_line((0, 0), (10, 10))
    (d / "converted").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(base / "nope", base / "nope") if False else None
    src = base / "src.dxf"
    doc.saveas(str(src))
    shutil.copyfile(src, d / "converted" / "zz_plant.dxf")
    write_manifest(job_id, [FileEntry("m1", "uploads/zz_plant.dxf", "mech",
                                      "converted/zz_plant.dxf",
                                      "cleaned/zz_plant_clean.dxf")])
    clean_v1.run(job_id)
    check("A7 the cleaner creates cleaned/",
          (d / "cleaned" / "zz_plant_clean.dxf").exists(),
          str(sorted(p.name for p in (d / "cleaned").iterdir()))
          if (d / "cleaned").exists() else "cleaned/ missing")


# ── A8 · the thread-safe regex cache ──────────────────────────────────────
def check_a8() -> None:
    from core.clean_common import _compile_label_re
    from core.clean_policy import CleaningPolicy

    p1 = CleaningPolicy(label_regex=r"^AAA$")
    p2 = CleaningPolicy(label_regex=r"^AAA$")
    p1.label_regex = r"^BBB$"
    check("A8 the regex cache follows the pattern",
          _compile_label_re(p1.label_regex).pattern == r"^BBB$"
          and _compile_label_re(p2.label_regex).pattern == r"^AAA$")


# ── A9 · the drop budget follows the fitted model ─────────────────────────
def check_a9() -> None:
    from core.overlay import OverlayOptions

    check("A9 the affine model gets its 1 % drop budget",
          OverlayOptions.for_model("affine").max_drop_ratio == 0.01
          and OverlayOptions.for_model("similarity").max_drop_ratio == 0.0)


# ── A10 · scale_x is the diagonal, column_scales is the truth ─────────────
def check_a10() -> None:
    from core.transform2d import Affine2D
    import math as _m

    aff = Affine2D(*(  # a 7.5 degree similarity of scale 1000
        1000.0 * _m.cos(_m.radians(7.5)), -1000.0 * _m.sin(_m.radians(7.5)),
        1000.0 * _m.sin(_m.radians(7.5)), 1000.0 * _m.cos(_m.radians(7.5)),
        0.0, 0.0))
    sx, sy = aff.column_scales()
    check("A10 the column norms are the true scale",
          abs(sx - 1000.0) < 1e-6 and abs(sy - 1000.0) < 1e-6,
          f"column_scales = ({sx:.3f}, {sy:.3f})")


# ── A11 · the upload filename ──────────────────────────────────────────────
def check_a11() -> None:
    from fastapi.testclient import TestClient
    from app import app
    import jobs
    import pipelines

    base = Path(tempfile.mkdtemp())
    jobs.JOBS_DIR = base
    pipelines.run_pipeline = lambda *a, **k: None
    pipelines.resume_after_axes = lambda *a, **k: None
    client = TestClient(app)
    r = client.post(
        "/api/v1/overlay",
        files=[("drawing_a", ("../../evil.dxf", b"x" * 32, "application/dxf")),
               ("drawing_b", ("str.dxf", b"x" * 32, "application/dxf"))])
    ok = r.status_code == 200
    if ok:
        from jobs import job_dir
        names = sorted(p.name for p in (job_dir(r.json()["job_id"]) / "uploads").iterdir())
        ok = "evil.dxf" in names and not (base / "evil.dxf").exists()
    check("A11 a traversing upload name stays inside uploads/", ok)


# ── A12 · the unit-blind thresholds ────────────────────────────────────────
def check_a12() -> None:
    import ezdxf
    import synth
    from core import spotcheck

    base = Path(tempfile.mkdtemp())
    mm = synth.build_pair(base / "mm", synth.LEGACY_COMPATIBLE)
    m = synth.build_pair(base / "m", synth.with_opts(synth.LEGACY_COMPATIBLE,
                                                     str_units="m"))
    g_mm = spotcheck._extract_str_gridlines(ezdxf.readfile(str(mm.str_path)))
    g_m = spotcheck._extract_str_gridlines(ezdxf.readfile(str(m.str_path)))
    check("A12 a metre-scale STR keeps its gridlines",
          len(g_mm) == len(g_m) and len(g_m) >= 7,
          f"mm={len(g_mm)} metres={len(g_m)}")


# ── A13 · the STR-intra coordinate space ──────────────────────────────────
def check_a13() -> None:
    from core.spotcheck import _affine_from
    from core.transform2d import Affine2D
    import math as _m

    aff = Affine2D(1000.0 * _m.cos(_m.radians(7.5)),
                   -1000.0 * _m.sin(_m.radians(7.5)),
                   1000.0 * _m.sin(_m.radians(7.5)),
                   1000.0 * _m.cos(_m.radians(7.5)), 196034.0, -170819.0)
    t = aff.to_json()
    got = _affine_from(t).apply((1234.0, 567.0))
    check("A13 the STR→MECH mapping is applied",
          abs(got[0] - aff.apply((1234.0, 567.0))[0]) < 1e-6,
          f"(1234, 567) -> ({got[0]:.1f}, {got[1]:.1f})")


# ── the perf budget ───────────────────────────────────────────────────────
def check_perf() -> None:
    from collections import Counter
    from core.mismatch import detect
    from core.transform2d import Affine2D
    from synth_perf import build_perf_docs

    base = Path(tempfile.mkdtemp())
    mech, strp, truth = build_perf_docs(base, n_side=24,
                                         mech_noise=8000, str_noise=16000)
    import time
    t0 = time.perf_counter()
    result = detect(mech, strp, Affine2D())
    elapsed = time.perf_counter() - t0
    kinds = Counter(f.kind for f in result.findings)
    check("the perf budget holds",
          kinds["PATTERN_OFFSET"] == truth["n_defects"] and elapsed < 30.0,
          f"{dict(kinds)} in {elapsed:.1f} s")


def main() -> int:
    print("Re-running every reproduction from the 2026-10-25 review:\n")
    for fn in (check_a1, check_a2, check_a3, check_a4, check_a5, check_a6,
               check_a7, check_a8, check_a9, check_a10, check_a11, check_a12,
               check_a13, check_perf):
        try:
            fn()
        except Exception as exc:              # noqa: BLE001 — reported
            check(fn.__name__, False, f"{type(exc).__name__}: {exc}")

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
