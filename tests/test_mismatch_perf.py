#!/usr/bin/env python3
"""tests/test_mismatch_perf.py — PLAN_MISMATCH §6.5, sub-step 8b.7.

The performance budget: on a drawing with ~170 000 entities the detector
must still find every seeded defect, and it must do it inside
``MISMATCH_PERF_BUDGET_S`` seconds (90 s by default).

The fixture (``tests/synth_perf.py``) is 71 × 71 = 5 041 bolt patterns
(~20 k bolts, ~20 k holes) plus 150 000 noise lines — 190 000 entities in
total — with 10 seeded pattern offsets.

This test is slow by construction: it is marked ``slow`` and it writes a
~29 MB pair of DXFs, so it is worth running before a release rather than
on every commit.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from synth_perf import build_perf_docs          # noqa: E402

pytestmark = pytest.mark.slow


def _budget() -> float:
    return float(os.environ.get("MISMATCH_PERF_BUDGET_S", "90"))


@pytest.fixture(scope="module")
def perf_docs(tmp_path_factory):
    """The fixture pair — built once for the whole module."""
    out = tmp_path_factory.mktemp("perf")
    return build_perf_docs(out)


def test_the_fixture_is_as_big_as_the_plan_says(perf_docs):
    """§6.5: ~170 000 entities, ~5 000 patterns, 10 seeded defects."""
    _mech, _strp, truth = perf_docs
    total = truth["n_entities_mech"] + truth["n_entities_str"]
    assert total > 170_000, total
    assert truth["n_patterns"] >= 5_000, truth["n_patterns"]
    assert truth["n_defects"] == 10


def test_the_detector_finds_every_seeded_defect_in_time(perf_docs):
    """Exactly 10 PATTERN_OFFSET findings, inside the budget."""
    from collections import Counter

    from core.mismatch import detect
    from core.transform2d import Affine2D

    mech, strp, truth = perf_docs

    t0 = time.perf_counter()
    result = detect(mech, strp, Affine2D())
    elapsed = time.perf_counter() - t0

    kinds = Counter(f.kind for f in result.findings)
    assert kinds["PATTERN_OFFSET"] == truth["n_defects"], (
        f"expected exactly {truth['n_defects']} seeded defects, got {dict(kinds)}")
    # and nothing else was invented
    assert set(kinds) == {"PATTERN_OFFSET"}, dict(kinds)

    budget = _budget()
    assert elapsed < budget, (
        f"detect() took {elapsed:.1f}s — the budget is {budget:.0f}s "
        f"(bolts: {result.timings_s.get('bolts', 0):.1f}s)")


def test_the_findings_land_where_they_were_seeded(perf_docs):
    """Every finding sits at one of the seeded defect positions."""
    from core.mismatch import detect
    from core.transform2d import Affine2D

    mech, strp, truth = perf_docs
    result = detect(mech, strp, Affine2D())

    seeded = truth["defect_centres"]
    for f in result.findings:
        x, y = f.location
        assert any(abs(x - sx) <= 1.0 and abs(y - sy) <= 1.0
                   for sx, sy in seeded), (
            f"the finding at ({x:.1f}, {y:.1f}) is not one of the seeded "
            "defect positions")


def test_the_bolt_check_scales(perf_docs):
    """The bolt check must not be quadratic in the number of bolts.

    The all-pairs pattern grouping was O(n²) — 200 million comparisons for
    20 000 bolts, and 74 of the 90 second budget.  This test pins the
    improvement: the bolt check must take a small fraction of the budget.
    """
    from core.mismatch import detect
    from core.transform2d import Affine2D

    mech, strp, _truth = perf_docs
    result = detect(mech, strp, Affine2D())
    bolts_s = result.timings_s.get("bolts", 0.0)
    budget = _budget()
    assert bolts_s < budget * 0.5, (
        f"the bolt check took {bolts_s:.1f}s of a {budget:.0f}s budget — "
        "the pattern grouping is probably quadratic again")
