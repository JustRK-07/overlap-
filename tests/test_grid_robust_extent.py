"""Robustness of grid detection against stray far-away entities.

A real CAD file routinely contains one INSERT parked far outside the plan
(a title block at UTM coordinates, an RTR marker, a sheet list). The length
threshold for candidate gridlines was scaled by the full drawing extent,
so a single such entity made the threshold unsatisfiable and the fit
reported zero shared intersections — one stray object silently killed the
whole pipeline. `_extent` is now measured from the bulk of the segment
endpoints instead.
"""

from __future__ import annotations

import ezdxf
import pytest

from core import grid
import synth

PREFIX = "TP104-"
SUFFIXES = ["A", "B", "C", "D", "1", "2", "3"]


def _detect_shared(str_path):
    """Shared (MECH, STR) grid-intersection keys for a STR file."""
    mech = ezdxf.readfile(str(str_path.with_name(
        str_path.name.replace("aa_frame", "zz_plant"))))
    sdoc = ezdxf.readfile(str(str_path))
    im = grid.intersections(grid.detect_grid(mech, PREFIX, SUFFIXES))
    isr = grid.intersections(grid.detect_grid(sdoc, PREFIX, SUFFIXES))
    return set(im) & set(isr), im, isr


def test_stray_entity_does_not_break_grid_detection(tmp_path):
    """A stray entity at 1e7 must not cost a single grid intersection."""
    clean = synth.build_pair(tmp_path / "clean", synth.HARD)
    base_shared, im_clean, isr_clean = _detect_shared(clean.str_path)
    assert len(base_shared) == 12

    # Same drawing plus one entity parked a million units away.
    strayed = synth.with_opts(synth.HARD, stray_str_entity_at=(1.0e7, 1.0e7))
    pair = synth.build_pair(tmp_path / "stray", strayed)

    sdoc = ezdxf.readfile(str(pair.str_path))
    ext = grid._extent(sdoc, grid._scan(sdoc)[1])
    # The robust window ignores the stray rather than scaling to it.
    assert ext < 1.0e6, f"extent still inflated by the stray: {ext}"

    shared, im, isr = _detect_shared(pair.str_path)
    assert len(shared) == 12
    assert shared == base_shared

    # And the grid is still geometrically right, not merely "found".
    # STR is drawn in metres here, so its intersections must be mapped
    # through the ground truth before comparing with MECH millimetres.
    for key in base_shared:
        mapped = pair.truth.apply((isr[key].x, isr[key].y))
        assert abs(mapped[0] - im[key].x) <= 1e-6 * 20000.0
        assert abs(mapped[1] - im[key].y) <= 1e-6 * 20000.0


def test_extent_unchanged_without_strays(tmp_path):
    """The robust window must not shrink the drawing on a normal file."""
    pair = synth.build_pair(tmp_path, synth.HARD)
    sdoc = ezdxf.readfile(str(pair.str_path))
    lines = grid._scan(sdoc)[1]
    ext = grid._extent(sdoc, lines)
    xs, ys = [], []
    for it in lines:
        for a, b in grid._entity_segments(it.entity):
            for p in (a, b):
                w = grid._world_pt(it.m, p)
                xs.append(w.x)
                ys.append(w.y)
    true_ext = max(max(xs) - min(xs), max(ys) - min(ys))
    # the window must not shrink the drawing — a 50 % tolerance would
    # have passed a window that lost half the extent
    assert ext == pytest.approx(true_ext, rel=0.05)