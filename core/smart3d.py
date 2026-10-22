"""core/smart3d.py — Smart3D "anchor pattern" helpers.

Smart3D-exported DXFs use a peculiar pattern: every INSERT shares the
same insert position and the same scale (e.g. (0,0) and (1,1)). Because
of that, block-local LINE coords are already world coords — applying
the insert+scale transform would squish all geometry onto a single point.

These helpers detect the pattern and walk block LINE segments correctly
in either case.

API:
    detect_anchor_pattern(doc) -> (is_anchor, ix, iy, sx, sy)
    segment_world_coords(ins)  -> ((ix, iy), (sx, sy))
    walk_block_lines(block, ins_insert, ins_scale, apply_transform=True)
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable


def detect_anchor_pattern(doc) -> tuple[bool, float, float, float, float]:
    """Detect Smart3D-style "anchor pattern": all INSERTs share the same
    insert + scale, indicating block LINE coords are already world coords.

    Returns (is_anchor, ix, iy, sx, sy). When is_anchor is True, callers
    should use block-local LINE coords directly (skip the insert+scale
    transform).
    """
    inserts = []
    scales = []
    for ins in doc.modelspace():
        if ins.dxftype() != "INSERT":
            continue
        try:
            ix, iy = ins.dxf.insert[0], ins.dxf.insert[1]
            sx = ins.dxf.xscale or 1.0
            sy = ins.dxf.yscale if ins.dxf.hasattr("yscale") and ins.dxf.yscale else sx
            inserts.append((round(ix, 2), round(iy, 2)))
            scales.append((sx, sy))
        except Exception:
            continue
    if not inserts or len(inserts) < 5:
        return False, 0.0, 0.0, 1.0, 1.0
    # If >90% of INSERTs share the same insert position AND the same scale,
    # this is the Smart3D "anchor pattern" — block coords are world coords.
    ic = Counter(inserts)
    sc = Counter(scales)
    top_insert, top_insert_n = ic.most_common(1)[0]
    top_scale, top_scale_n = sc.most_common(1)[0]
    if top_insert_n / len(inserts) > 0.9 and top_scale_n / len(scales) > 0.9:
        return True, top_insert[0], top_insert[1], top_scale[0], top_scale[1]
    return False, 0.0, 0.0, 1.0, 1.0


def segment_world_coords(ins) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return ((ix, iy), (sx, sy)) for an INSERT — applying insert + xscale.

    ezdxf block LINE coords are local — callers compose these with
    walk_block_lines(..., apply_transform=True) to reach world coords.

    When the doc uses the Smart3D anchor pattern, pass (0,0) and (1,1)
    with apply_transform=False instead.
    """
    try:
        sx = ins.dxf.xscale or 1.0
        sy = ins.dxf.yscale if ins.dxf.hasattr("yscale") and ins.dxf.yscale else sx
    except Exception:
        sx, sy = 1.0, 1.0
    try:
        ix, iy = ins.dxf.insert[0], ins.dxf.insert[1]
    except Exception:
        ix, iy = 0.0, 0.0
    return (ix, iy), (sx, sy)


def walk_block_lines(block,
                     ins_insert: tuple[float, float],
                     ins_scale: tuple[float, float],
                     apply_transform: bool = True,
                     ) -> Iterable[tuple[tuple[float, float], tuple[float, float]]]:
    """Yield ((x1, y1), (x2, y2)) LINE segments from a block.

    apply_transform=False: return block-local coords as-is (anchor pattern).
    """
    ix, iy = ins_insert
    sx, sy = ins_scale
    for e in block:
        if e.dxftype() != "LINE":
            continue
        try:
            s, end = e.dxf.start, e.dxf.end
            if apply_transform:
                yield ((ix + sx * s[0], iy + sy * s[1]),
                       (ix + sx * end[0], iy + sy * end[1]))
            else:
                yield ((s[0], s[1]), (end[0], end[1]))
        except Exception:
            continue
