#!/usr/bin/env python3
"""test_mismatch_synth.py — the §6.3 detection tests, on the synthetic scene.

``tests/synth.py`` authors all 18 cases of PLAN_MISMATCH.md §6.1 with exact
ground truth (derived analytically, never read back out of a DXF), so these
tests do not ask "did the detector find *something*" — they ask whether the
finding that came back is **the** finding the fixture drew, to a millimetre,
and whether anything at all was invented on top.

Every test is parametrized over ``MISMATCH`` (mm, axis-aligned) and
``MISMATCH_HARD`` (metres, 7.5° rotated, rotated grid INSERTs), because a
detector that only works in one unit system, or only on unrotated INSERTs, is
not a detector.

One thing the plan leaves to the detector is *which* of two tied candidates
gets named: a skid hanging 150 mm over a plinth edge is 150 mm out at **both**
right-hand corners, so ``OUTSIDE_OUTLINE`` / ``EDGE_DISTANCE`` may legitimately
report either — see :func:`_loc_candidates`.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import ezdxf
import numpy as np
import pytest
from ezdxf import transform as ez_transform
from ezdxf.math import Matrix44

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import synth                                                    # noqa: E402
from core.mismatch import detect                                # noqa: E402
from core.mismatch.model import SEVERITY_RANK, rank_key         # noqa: E402
from core.transform2d import Affine2D                           # noqa: E402

#: §6.3 — the presets the whole §6.3 list is parametrized over.
PRESETS = ("MISMATCH", "MISMATCH_HARD")

#: §6.3 tolerances — "location within 1 mm, magnitude within 5 %".
LOC_TOL_MM = 1.0
MAG_REL = 0.05
MAG_ABS = 1.0                        # …but never tighter than this
OFFSET_COS = 0.99
ROT_REL = 0.05
DETAIL_REL = 0.05

#: The two containment kinds whose reported vertex is an exact tie.
CONTAINMENT_KINDS = ("OUTSIDE_OUTLINE", "EDGE_DISTANCE")

#: What §1.3 says the coverage counters must add up to on the full scene.
#: ``items`` is every feature the check was handed, ``matched`` the ones it
#: found a counterpart for, ``unmatched`` the difference.
EXPECTED_COVERAGE = {
    "bolts": {"items": 37, "matched": 32, "unmatched": 5},
    "support": {"items": 7, "matched": 6, "unmatched": 1},
    "containment": {"items": 3, "matched": 3, "unmatched": 0},
    "clash": {"items": 5, "matched": 1, "unmatched": 4},
}


# ─────────────────────────────────────────────────────────────────────────────
# fixtures and helpers
# ─────────────────────────────────────────────────────────────────────────────

_PAIRS: dict = {}


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    return tmp_path_factory.mktemp("mismatch")


def _pair(preset, workdir, **kw):
    """``synth.build_pair`` once per (preset, option) and cached.

    Building a pair writes two DXFs and re-reads them, so the eight tests
    below share one build per preset rather than paying for eight.
    """
    key = (preset, tuple(sorted(kw.items())))
    if key not in _PAIRS:
        opts = getattr(synth, preset)
        if kw:
            opts = synth.with_opts(opts, **kw)
        slug = preset + "".join(f"-{k}-{v}" for k, v in sorted(kw.items()))
        _PAIRS[key] = synth.build_pair(workdir / slug, opts)
    return _PAIRS[key]


def _detect(pair, **kw):
    """``detect()`` on a built pair, using the pair's own ground-truth fit."""
    policy = {"max_findings": 10_000}
    policy.update(kw.pop("policy", None) or {})
    return detect(pair.mech_path, pair.str_path,
                  Affine2D(*pair.truth.as_affine()), policy=policy, **kw)


def _dist(a, b) -> float:
    return float(math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1])))


def _case_row_y(pair) -> float:
    """y of a scene cell row, from ``SynthOptions`` alone."""
    opts = pair.opts
    return float(opts.scene_origin[1]) + float(opts.scene_pitch) \
        + float(opts.mech_offset[1])


def _loc_candidates(e, pair) -> list:
    """Where the finding for one expectation is allowed to sit.

    A plain case has exactly one legal location.  A containment case has
    two: a skid overhanging a plinth is the same distance outside at both
    right-hand corners, the fixture names the upper corner for
    ``OUTSIDE_OUTLINE`` and the lower one for ``EDGE_DISTANCE`` (§ the
    ``_outside_case`` comment in ``synth.py``), and the detector reports
    whichever its vertex order reaches first.  Which one is named is a
    tie-break, not a defect, so the mirror image in the case's own cell row
    is offered as well — every scene row, since the row index is not part of
    an ``ExpectedFinding``.  A wide candidate list cannot let one finding
    satisfy two expectations: ``_match`` still requires the kinds to be equal.
    """
    base = (float(e.location_mech[0]), float(e.location_mech[1]))
    if e.kind not in CONTAINMENT_KINDS:
        return [base]
    y0 = float(pair.opts.scene_origin[1]) + float(pair.opts.mech_offset[1])
    pitch = float(pair.opts.scene_pitch)
    return [base] + [(base[0], 2.0 * (y0 + r * pitch) - base[1])
                     for r in range(len(synth.ALL_MISMATCH_CASES))]


def _match(report, pair) -> tuple:
    """Greedy one-to-one match of findings to expectations.

    Returns ``(matched, leftover)`` where ``matched`` is a list of
    ``(finding, expectation)`` in expectation order and ``leftover`` the
    findings nothing claimed.  One finding may satisfy at most one
    expectation and vice versa, which is what makes
    ``test_each_defect_detected`` a bijection rather than a subset check.
    """
    free = list(report.findings)
    matched: list = []
    for e in pair.mismatch_expected:
        for f in free:
            if f.kind == e.kind and any(
                    _dist(f.location, c) <= LOC_TOL_MM
                    for c in _loc_candidates(e, pair)):
                free.remove(f)
                matched.append((f, e))
                break
    return matched, free


def _expectation(pair, case, index=0) -> synth.ExpectedFinding:
    hits = [e for e in pair.mismatch_expected if e.case == case]
    assert len(hits) > index, f"fixture has no {case}[{index}]"
    return hits[index]


# ─────────────────────────────────────────────────────────────────────────────
# the headline test
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
@pytest.mark.parametrize("preset", PRESETS)
def test_mismatch_synthetic_defects(preset, workdir):
    """Every authored defect comes back, correctly, and the controls stay quiet.

    This is the test 8b.1 left as a strict ``xfail``; ``detect()`` now exists,
    so it is a real assertion and every other test in §6.3 narrows one of its
    claims.
    """
    pair = _pair(preset, workdir)
    report = _detect(pair)

    matched, extra = _match(report, pair)
    assert len(matched) == len(pair.mismatch_expected), \
        "missed: " + ", ".join(
            f"{e.case}/{e.kind}" for e in pair.mismatch_expected
            if not any(e is got for _f, got in matched))
    assert not extra, [f.id + "/" + f.kind for f in extra]

    for f, e in matched:
        # the finding must be AT LEAST as severe as the expectation.  The
        # old inequality was the other way round, which made the check
        # vacuous for the 12 CRITICAL expectations (rank >= 0 always holds)
        # — severity was effectively untested across the whole subsystem.
        assert SEVERITY_RANK.get(f.severity, 99) <= SEVERITY_RANK[e.severity], \
            (e.case, e.severity, f.severity)
        assert any(_dist(f.location, c) <= LOC_TOL_MM
                   for c in _loc_candidates(e, pair)), (e.case, f.location,
                                                        e.location_mech)

    for ctl in pair.mismatch_controls:
        for f in report.findings:
            assert _dist(f.location, ctl.center_mech) > ctl.radius_mm, \
                (ctl.case, f.id, f.kind)


# ─────────────────────────────────────────────────────────────────────────────
# §6.3
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
@pytest.mark.parametrize("preset", PRESETS)
def test_each_defect_detected(preset, workdir):
    """One finding per ExpectedFinding, with every numeric field on target."""
    pair = _pair(preset, workdir)
    report = _detect(pair)
    matched, extra = _match(report, pair)
    assert len(matched) == len(pair.mismatch_expected), \
        "missed: " + ", ".join(
            f"{e.case}/{e.kind}" for e in pair.mismatch_expected
            if not any(e is got for _f, got in matched))

    for f, e in matched:
        where = f"{f.id} {e.case}/{e.kind}"

        assert f.kind == e.kind, where
        # at least as severe as the expectation (see the note above)
        assert SEVERITY_RANK.get(f.severity, 99) <= SEVERITY_RANK[e.severity], \
            (where, e.severity, f.severity)

        assert any(_dist(f.location, c) <= LOC_TOL_MM
                   for c in _loc_candidates(e, pair)), \
            (where, "location", f.location, e.location_mech)

        if e.magnitude_mm is not None:
            got = f.magnitude()
            assert abs(got - e.magnitude_mm) <= max(MAG_ABS,
                                                    MAG_REL * e.magnitude_mm), \
                (where, "magnitude_mm", got, e.magnitude_mm)

        if e.offset_mm is not None:
            want = np.asarray(e.offset_mm, dtype=float)
            got = np.asarray(f.offset_mm if f.offset_mm is not None else (0.0, 0.0))
            if np.linalg.norm(want) > 1e-9:
                cos = float(want @ got) / (np.linalg.norm(want) * np.linalg.norm(got))
                assert cos >= OFFSET_COS, (where, "offset_mm", got.tolist(),
                                           want.tolist(), cos)
            else:
                assert np.linalg.norm(got) <= MAG_ABS, (where, "offset_mm",
                                                        got.tolist())

        if e.rotation_deg is not None:
            assert f.rotation_deg is not None, (where, "rotation_deg")
            assert abs(f.rotation_deg - e.rotation_deg) <= \
                max(ROT_REL * abs(e.rotation_deg), 1e-6), \
                (where, "rotation_deg", f.rotation_deg, e.rotation_deg)

        for key, want in (e.details or {}).items():
            got = f.details.get(key)
            assert got is not None, (where, f"details[{key!r}] missing")
            assert abs(float(got) - float(want)) <= \
                max(DETAIL_REL * abs(float(want)), 1e-9), \
                (where, f"details[{key!r}]", got, want)


@pytest.mark.slow
@pytest.mark.parametrize("preset", PRESETS)
def test_no_false_positives(preset, workdir):
    """Nothing is reported that the fixture did not draw.

    Two halves: every finding is claimed by an expectation (a bijection — so a
    detector cannot pass by finding all 13 defects *and* inventing 40), and
    nothing lands inside a control region, which is where the fixture put
    clean geometry that looks almost identical to a defect.
    """
    pair = _pair(preset, workdir)
    report = _detect(pair)
    matched, extra = _match(report, pair)

    assert not extra, "unclaimed findings: " + ", ".join(
        f"{f.id} {f.kind}@{f.location[0]:.1f},{f.location[1]:.1f}" for f in extra)
    assert len(matched) == len(pair.mismatch_expected)
    assert len(report.findings) == len(pair.mismatch_expected), \
        f"{len(report.findings)} findings for {len(pair.mismatch_expected)} defects"

    for ctl in pair.mismatch_controls:
        d = sorted(_dist(f.location, ctl.center_mech) for f in report.findings)
        inside = [f for f in report.findings
                  if _dist(f.location, ctl.center_mech) <= ctl.radius_mm]
        assert not inside, (ctl.case, [(f.id, f.kind) for f in inside])


@pytest.mark.slow
@pytest.mark.parametrize("preset", PRESETS)
def test_pattern_offset_single_finding(preset, workdir):
    """A shifted bolt *pattern* is one PATTERN_OFFSET, not four bolt errors.

    8 mm is far inside the 150 mm search radius, so every bolt still finds its
    hole; the information the drawing carries is that the whole array moved,
    and reporting it per-bolt would bury one 8 mm array defect under four 8 mm
    bolt defects.
    """
    pair = _pair(preset, workdir)
    report = _detect(pair)
    e = _expectation(pair, "pattern_offset")

    near = [f for f in report.findings
            if f.kind == "PATTERN_OFFSET" and _dist(f.location, e.location_mech) <= LOC_TOL_MM]
    assert len(near) == 1, [(f.id, f.kind, f.location) for f in near]
    assert near[0].magnitude() == pytest.approx(e.magnitude_mm, abs=MAG_ABS)
    assert near[0].details["n_bolts"] == 4 and near[0].details["n_assigned"] == 4

    loose = [f for f in report.findings
             if f.kind == "BOLT_HOLE_OFFSET"
             and _dist(f.location, e.location_mech) <= 600.0]
    assert not loose, [(f.id, f.location) for f in loose]

    # the same must hold for the 8-bolt flange array
    e2 = _expectation(pair, "flange_pcd_offset")
    near2 = [f for f in report.findings
             if f.kind == "PATTERN_OFFSET" and _dist(f.location, e2.location_mech) <= LOC_TOL_MM]
    assert len(near2) == 1
    assert near2[0].details["n_bolts"] == 8


@pytest.mark.slow
@pytest.mark.parametrize("preset", PRESETS)
def test_coverage_counts(preset, workdir):
    """``coverage`` accounts for every item each check looked at.

    ``items == matched + unmatched`` is the invariant that matters; the exact
    numbers pin down that nothing was silently skipped — 37 bolts seen is 37
    bolts looked at, not 32.
    """
    pair = _pair(preset, workdir)
    report = _detect(pair)

    assert report.coverage == EXPECTED_COVERAGE, report.coverage
    for check, counts in report.coverage.items():
        assert counts["items"] == counts["matched"] + counts["unmatched"], check

    # both drawings were actually read, in both directions
    assert report.feature_counts["MECH"]["mech_bolt"] == 33
    assert report.feature_counts["MECH"]["mech_plate_hole"] == 4
    assert report.feature_counts["STR"]["str_hole"] == 32
    assert report.feature_counts["STR"]["str_anchor"] == 4
    assert set(report.coverage) == {"bolts", "support", "containment", "clash"}


@pytest.mark.slow
@pytest.mark.parametrize("preset", PRESETS)
def test_ranking_order(preset, workdir):
    """Findings come back ranked, numbered ``MM-0001`` upwards, worst first."""
    pair = _pair(preset, workdir)
    report = _detect(pair)
    findings = list(report.findings)

    assert [f.id for f in findings] == \
        ["MM-%04d" % i for i in range(1, len(findings) + 1)]
    assert [rank_key(f) for f in findings] == sorted(rank_key(f) for f in findings)

    ranks = [SEVERITY_RANK[f.severity] for f in findings]
    assert ranks == sorted(ranks), [(f.id, f.severity) for f in findings]

    # CRITICAL before WARNING, and the one WARNING (EDGE_DISTANCE, 20 mm
    # short of a 50 mm rule) is last because nothing ranks below it
    assert findings[0].severity == "CRITICAL"
    assert [f.severity for f in findings] == \
        sorted((f.severity for f in findings), key=lambda s: SEVERITY_RANK[s])
    assert findings[-1].kind == "EDGE_DISTANCE"

    summary = report.summary()
    assert summary["total"] == len(findings)
    assert sum(summary["by_severity"].values()) == len(findings)


@pytest.mark.slow
@pytest.mark.parametrize("preset", PRESETS)
def test_deterministic_and_order_independent(preset, workdir):
    """Same inputs → the same report; a different drawing order → the same report.

    Two runs must be byte-identical, and reversing the order the scene is
    drawn in (which reverses every tie-break the extractor and the checks
    could have leaned on) must not move a single finding.
    """
    pair = _pair(preset, workdir)
    first, second = _detect(pair), _detect(pair)
    assert _fingerprint(first) == _fingerprint(second)

    rev = _pair(preset, workdir, scene_reverse_order=True)
    rev_report = _detect(rev)
    assert sorted(_fingerprint(rev_report)[0]) == sorted(_fingerprint(first)[0])
    assert rev_report.coverage == first.coverage

    # truncation is honoured and is visible, so a rerun knows it lost rows
    small = detect(pair.mech_path, pair.str_path,
                   Affine2D(*pair.truth.as_affine()),
                   policy={"max_findings": 3})
    assert small.truncated is True and len(small.findings) == 3
    assert [f.id for f in small.findings] == \
        [f.id for f in first.findings[:3]]


def _fingerprint(report):
    """Every reported number, rounded — order is asserted separately."""
    return (
        [(f.id, f.uid, f.kind, f.severity,
          round(float(f.location[0]), 6), round(float(f.location[1]), 6),
          round(float(f.magnitude()), 6),
          None if f.offset_mm is None else (round(f.offset_mm[0], 6),
                                           round(f.offset_mm[1], 6)),
          None if f.rotation_deg is None else round(f.rotation_deg, 6),
          f.systematic, f.within_fit_uncertainty)
         for f in report.findings],
        report.feature_counts,
    )


@pytest.mark.slow
def test_mech_units_metres(workdir):
    """A MECH drawing in metres reports exactly the same millimetres.

    Everything the checks compute is in MECH drawing units; only the report
    is in mm, via ``mm_per_unit``.  Re-saving the MECH file in metres must
    therefore move every number in ``Finding`` but none of them *meaningfully*.
    """
    pair = _pair("MISMATCH", workdir)
    mm_report = _detect(pair)

    metres_path = Path(workdir) / "plant_metres.dxf"
    doc = ezdxf.readfile(str(pair.mech_path))
    doc.header["$INSUNITS"] = 6                        # metres
    ez_transform.inplace(list(doc.modelspace()),
                         Matrix44.scale(0.001, 0.001, 0.001))
    doc.saveas(metres_path)

    a, b, c, d, tx, ty = pair.truth.as_affine()
    m_report = detect(metres_path, pair.str_path,
                      Affine2D(a * 0.001, b * 0.001, c * 0.001, d * 0.001,
                               tx * 0.001, ty * 0.001),
                      policy={"max_findings": 10_000})

    assert m_report.mech_insunits == 6
    assert m_report.mm_per_unit == pytest.approx(1000.0)
    assert m_report.units_assumed is False

    assert [(f.kind, f.severity) for f in m_report.findings] == \
        [(f.kind, f.severity) for f in mm_report.findings]
    for mf, f in zip(m_report.findings, mm_report.findings):
        assert mf.magnitude() == pytest.approx(f.magnitude(), abs=1e-6), mf.id
        assert mf.tolerance_mm == pytest.approx(f.tolerance_mm, abs=1e-6), mf.id
        assert mf.location == pytest.approx(
            tuple(v * 0.001 for v in f.location), abs=1e-6), mf.id
    assert m_report.coverage == mm_report.coverage


@pytest.mark.slow
def test_policy_override_changes_result(workdir):
    """A partial policy dict overrides exactly its own section and no other."""
    pair = _pair("MISMATCH", workdir)
    base = _detect(pair)
    assert any(f.kind == "CLASH" for f in base.findings)
    assert any(f.kind == "OUTSIDE_OUTLINE" for f in base.findings)

    no_clash = _detect(pair, policy={"checks": {"clash": False}})
    assert not any(f.kind == "CLASH" for f in no_clash.findings)
    assert no_clash.coverage["clash"]["items"] == 0
    assert no_clash.coverage["bolts"] == base.coverage["bolts"]
    assert len(no_clash.findings) == len(base.findings) - 1
    assert no_clash.policy_hash != base.policy_hash

    # 150 mm overhang, 400 mm tolerance: outside, but not a defect
    slack = _detect(pair, policy={"tol_mm": {"outline": 400.0}})
    assert not any(f.kind == "OUTSIDE_OUTLINE" for f in slack.findings)
    assert any(f.kind == "EDGE_DISTANCE" for f in slack.findings)   # that section kept its 50 mm

    # a partial dict keeps the rest of the section it overrides
    assert slack.policy.tol.support == base.policy.tol.support
    assert slack.policy.tol.min_edge == base.policy.tol.min_edge
    assert slack.policy.tol.outline == 400.0

    # a bad override is refused, not ignored
    with pytest.raises(Exception):
        _detect(pair, policy={"tol_mm": {"nonsense": 1.0}})


@pytest.mark.slow
def test_global_shift_systematic(workdir):
    """A drawing-wide shift is one SYSTEMATIC_OFFSET, not 16 defects.

    ``MISMATCH_GLOBAL`` shifts every MECH pattern 25 mm along +x and adds one
    *local* 8 mm defect on top.  PLAN_MISMATCH §3 says the neighbourhood
    median is subtracted from each item first: the fifteen 25 mm items cancel
    to nothing and survive as INFO ``systematic=True``, the one 8 mm item does
    not and stays a CRITICAL, and the fifteen coherent ones become a single
    WARNING SYSTEMATIC_OFFSET.
    """
    pair = _pair("MISMATCH_GLOBAL", workdir)
    report = _detect(pair)

    assert len(report.findings) == 17
    assert report.summary()["by_severity"] == {"CRITICAL": 1, "WARNING": 1,
                                                "INFO": 15}

    systematic = [f for f in report.findings if f.systematic]
    assert len(systematic) == 15
    assert all(f.severity == "INFO" and f.kind == "PATTERN_OFFSET"
               for f in systematic)
    assert all(f.magnitude() == pytest.approx(25.0, abs=MAG_ABS) for f in systematic)
    assert all(f.local_magnitude_mm == pytest.approx(0.0, abs=MAG_ABS)
               for f in systematic), "the neighbour median must cancel the shift"

    local = [f for f in report.findings if not f.systematic and f.kind != "SYSTEMATIC_OFFSET"]
    assert len(local) == 1
    assert local[0].severity == "CRITICAL"
    assert local[0].magnitude() == pytest.approx(
        math.hypot(25.0, 8.0), abs=MAG_ABS)
    assert local[0].local_magnitude_mm == pytest.approx(8.0, abs=MAG_ABS)
    assert local[0].ratio == pytest.approx(8.0 / 1.5, rel=1e-6)

    cluster = [f for f in report.findings if f.kind == "SYSTEMATIC_OFFSET"]
    assert len(cluster) == 1
    assert cluster[0].severity == "WARNING"
    assert cluster[0].details["n_items"] == 15
    assert cluster[0].details["global"] is True
    assert cluster[0].magnitude() == pytest.approx(25.0, abs=MAG_ABS)
    assert cluster[0].offset_mm[0] == pytest.approx(-25.0, abs=MAG_ABS)
    assert cluster[0].offset_mm[1] == pytest.approx(0.0, abs=MAG_ABS)
    assert "grids agree" in cluster[0].message