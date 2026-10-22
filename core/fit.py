#!/usr/bin/env python3
"""core/fit.py — Stage 6.

Find the gridline LINE positions (not text positions) in both cleaned
drawings, then solve a least-squares affine that maps STR coords →
MECH coords.

Inputs:
  - cleaned/<base>_clean.dxf  (MECH — alphabetical order)
  - cleaned/<base>_clean.dxf  (STR  — alphabetical order)
  - axes/convention.json      (user's chosen prefix + axes)

Outputs:
  - transform.json with scale_x, scale_y, tx, ty + per-intersection residuals
  - persisted into jobs.stages

Ported from ISGEC/A/scripts/fit.py with paths parameterized and grid-label
regex built from the user's chosen convention (no hardcoded TP104-*).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import ezdxf

from jobs import job_dir


# AutoCAD Mechanical centerline layer names (MECH centerlines)
GRID_LAYERS_MECH = {
    "CEN", "CENETR LINE", "CENTER", "PHANTOM", "PANTHOM",
    "HIDDEN", "HIDDDEN", "CENTERLINE", "C-ROAD-STAN",
}


def _is_vertical(e) -> bool:
    s, e2 = e.dxf.start, e.dxf.end
    return abs(s[0] - e2[0]) < 1.0 and abs(s[1] - e2[1]) > 100.0


def _is_horizontal(e) -> bool:
    s, e2 = e.dxf.start, e.dxf.end
    return abs(s[1] - e2[1]) < 1.0 and abs(s[0] - e2[0]) > 100.0


def _build_grid_re(prefix: str, suffixes: list[str]) -> re.Pattern:
    """Build a regex matching prefix + suffix, case-insensitive on the suffix."""
    # Escape prefix for regex; build alternatives for each suffix
    prefix_esc = re.escape(prefix)
    sfx_alt = "|".join(re.escape(s) for s in suffixes)
    return re.compile(rf"^{prefix_esc}\s*({sfx_alt})\s*$", re.IGNORECASE)


def _find_mech_axis(mech_path: Path, label_suffix: str, axis: str, grid_re: re.Pattern):
    """Return (axis_coord, distance_to_label) for the MECH gridline.

    For axis='X' (letter labels): returns X coord of vertical line near the TEXT.
    For axis='Y' (number labels): returns Y coord of horizontal line near the TEXT.
    """
    doc = ezdxf.readfile(str(mech_path))
    msp = doc.modelspace()

    text_pos = None
    for t in msp:
        if t.dxftype() == "TEXT":
            m = grid_re.match(t.dxf.text.strip())
            if m and m.group(1).upper() == label_suffix.upper():
                text_pos = (t.dxf.insert[0], t.dxf.insert[1])
                break
    if text_pos is None:
        return None, None

    is_vert = (axis == "X")
    best = None
    best_dist = float("inf")
    for e in msp:
        if e.dxftype() != "LINE":
            continue
        if e.dxf.layer not in GRID_LAYERS_MECH:
            continue
        if is_vert and not _is_vertical(e):
            continue
        if not is_vert and not _is_horizontal(e):
            continue
        s, e2 = e.dxf.start, e.dxf.end
        mx = (s[0] + e2[0]) / 2
        my = (s[1] + e2[1]) / 2
        d = ((mx - text_pos[0]) ** 2 + (my - text_pos[1]) ** 2) ** 0.5
        if d < best_dist:
            best_dist = d
            best = e
    if best is None:
        return None, None
    s, e2 = best.dxf.start, best.dxf.end
    if axis == "X":
        return (s[0] + e2[0]) / 2, best_dist
    return (s[1] + e2[1]) / 2, best_dist


def _find_str_axis(str_path: Path, label_suffix: str, axis: str, grid_re: re.Pattern):
    """Find the STR gridline by walking GridLine-* INSERTs.

    For each candidate block:
      - find a TEXT matching grid_re
      - find the longest axis-aligned LINE
      - apply INSERT's position + scale to convert to world coords
    Returns (axis_coord, 0.0).
    """
    doc = ezdxf.readfile(str(str_path))
    msp = doc.modelspace()

    is_vert = (axis == "X")

    for ins in msp:
        if ins.dxftype() != "INSERT":
            continue
        nm = ins.dxf.name
        if not (nm.startswith("GridLine-") or nm.startswith(" Line-")
                or nm.startswith("_STR_GridLine-") or nm.startswith("_STR_Line-")):
            continue

        # Find block def (search by name)
        block = None
        for b in doc.blocks:
            if b.name == ins.dxf.name:
                block = b
                break
        if block is None:
            continue

        text_label = None
        best_line = None
        best_length = 0.0
        for e in block:
            if e.dxftype() == "TEXT":
                m = grid_re.match(e.dxf.text.strip())
                if m and m.group(1).upper() == label_suffix.upper():
                    text_label = e
            elif e.dxftype() == "LINE":
                s, e2 = e.dxf.start, e.dxf.end
                length = ((e2[0] - s[0]) ** 2 + (e2[1] - s[1]) ** 2) ** 0.5
                if is_vert and _is_vertical(e) and length > best_length:
                    best_length = length
                    best_line = e
                elif not is_vert and _is_horizontal(e) and length > best_length:
                    best_length = length
                    best_line = e

        if text_label is None or best_line is None:
            continue

        s, e2 = best_line.dxf.start, best_line.dxf.end
        px, py = ins.dxf.insert[0], ins.dxf.insert[1]
        sx = ins.dxf.xscale or 1.0
        sy = ins.dxf.yscale if ins.dxf.hasattr("yscale") and ins.dxf.yscale else sx

        if axis == "X":
            wx1 = px + sx * s[0]
            wx2 = px + sx * e2[0]
            return (wx1 + wx2) / 2, 0.0
        wy1 = py + sy * s[1]
        wy2 = py + sy * e2[1]
        return (wy1 + wy2) / 2, 0.0

    # Pass 2 (fallback): the cleaner may have stripped the INSERTs but
    # the block defs + their labels usually survive. Walk every non-
    # anonymous block def directly looking for label + axis-aligned LINE.
    for block in doc.blocks:
        if block.name.startswith("*"):
            continue
        if not (block.name.startswith("GridLine-") or block.name.startswith(" Line-")
                or block.name.startswith("_STR_GridLine-") or block.name.startswith("_STR_Line-")):
            continue
        text_label = None
        best_line = None
        best_length = 0.0
        for e in block:
            if e.dxftype() == "TEXT":
                m = grid_re.match(e.dxf.text.strip())
                if m and m.group(1).upper() == label_suffix.upper():
                    text_label = e
            elif e.dxftype() == "LINE":
                s, e2 = e.dxf.start, e.dxf.end
                length = ((e2[0] - s[0]) ** 2 + (e2[1] - s[1]) ** 2) ** 0.5
                if is_vert and _is_vertical(e) and length > best_length:
                    best_length = length
                    best_line = e
                elif not is_vert and _is_horizontal(e) and length > best_length:
                    best_length = length
                    best_line = e
        if text_label is None or best_line is None:
            continue
        s, e2 = best_line.dxf.start, best_line.dxf.end
        if axis == "X":
            return (s[0] + e2[0]) / 2, 0.0
        return (s[1] + e2[1]) / 2, 0.0

    return None, None


def _fit_axis(str_vals: list[float], mech_vals: list[float]):
    """Least-squares fit: mech = a * str + b."""
    n = len(str_vals)
    sx = sum(str_vals)
    sy = sum(mech_vals)
    sxx = sum(v * v for v in str_vals)
    sxy = sum(s * m for s, m in zip(str_vals, mech_vals))
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return 1.0, (sy - sx) / n
    a = (n * sxy - sx * sy) / denom
    b = (sy - a * sx) / n
    return a, b


def run(job_id: str, *, pair_dir = None) -> None:
    """Stage 6 entrypoint.

    `pair_dir` is the per-pair `cleaned/` directory
    (`jobs/<id>/out/pairs/<pair_id>/cleaned/`) for Slice-1 multi-pair
    runs. When `None` (the legacy default), behaviour is bit-equivalent
    to the original single-pair pipeline. When set, every output is
    written under `pair_dir.parent` (the pair root) instead of `out/`.
    """
    d = job_dir(job_id)
    if pair_dir is not None:
        root = Path(pair_dir).parent
        cleaned = sorted(Path(pair_dir).glob("*_clean.dxf"))
    else:
        root = d
        cleaned = sorted((d / "cleaned").glob("*_clean.dxf"))
    if len(cleaned) != 2:
        raise RuntimeError(f"Need 2 cleaned DXFs; found {len(cleaned)}")
    mech_path = cleaned[0]   # alphabetical: MECH first
    str_path = cleaned[1]

    # Read user's convention (legacy: jobs/<id>/axes/convention.json
    # pair-scoped: <pair_root>/axes/convention.json)
    convention_path = root / "axes" / "convention.json"
    if not convention_path.exists():
        raise RuntimeError("axes/convention.json missing — user must confirm axes first")
    convention = json.loads(convention_path.read_text())
    prefix = convention["prefix"]
    axes = convention["axes"]

    # Sort axes: letters (X-axis labels) and numbers (Y-axis labels)
    x_labels = [a for a in axes if a.isalpha()]
    y_labels = [a for a in axes if a.isdigit()]

    grid_re = _build_grid_re(prefix, axes)

    # Find paired axes (X for letters, Y for numbers)
    x_pairs = []
    for lbl in x_labels:
        sx, _ = _find_str_axis(str_path, lbl, "X", grid_re)
        mx, _ = _find_mech_axis(mech_path, lbl, "X", grid_re)
        if sx is None or mx is None:
            continue
        x_pairs.append((lbl, sx, mx))

    y_pairs = []
    for lbl in y_labels:
        sy, _ = _find_str_axis(str_path, lbl, "Y", grid_re)
        my, _ = _find_mech_axis(mech_path, lbl, "Y", grid_re)
        if sy is None or my is None:
            continue
        y_pairs.append((lbl, sy, my))

    if len(x_pairs) < 2 or len(y_pairs) < 2:
        raise RuntimeError(
            f"Not enough axis pairs: x={len(x_pairs)}, y={len(y_pairs)}. "
            "Need at least 2 of each."
        )

    # Solve affine
    ax, bx = _fit_axis([s for _, s, _ in x_pairs], [m for _, _, m in x_pairs])
    ay, by = _fit_axis([s for _, s, _ in y_pairs], [m for _, _, m in y_pairs])

    # Per-intersection residuals
    residuals = []
    for xlbl, sx, mx in x_pairs:
        for ylbl, sy, my in y_pairs:
            pred = (ax * sx + bx, ay * sy + by)
            d2d = ((pred[0] - mx) ** 2 + (pred[1] - my) ** 2) ** 0.5
            residuals.append((f"{xlbl}-{ylbl}", sx, sy, mx, my, pred[0], pred[1], d2d))
    rms = (sum(r ** 2 for _, _, _, _, _, _, _, r in residuals) / len(residuals)) ** 0.5
    max_r = max(r for _, _, _, _, _, _, _, r in residuals)

    transform = {
        "scale_x": ax,
        "scale_y": ay,
        "tx": bx,
        "ty": by,
        "x_labels": [{"label": l, "str": s, "mech": m} for l, s, m in x_pairs],
        "y_labels": [{"label": l, "str": s, "mech": m} for l, s, m in y_pairs],
        "intersections": [
            {"label": l, "str": [sx, sy], "mech": [mx, my],
             "predicted_mech": [px, py], "residual": r}
            for (l, sx, sy, mx, my, px, py, r) in residuals
        ],
        "rms_residual": rms,
        "max_residual": max_r,
        "convention": {"prefix": prefix, "axes": axes},
    }

    transform_path = root / "transform.json"
    transform_path.write_text(json.dumps(transform, indent=2))
