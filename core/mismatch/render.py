#!/usr/bin/env python3
"""core/mismatch/render.py — the ``QA|MISMATCH*`` markers.

PLAN_MISMATCH §4.  Every finding is drawn into the superimposed DXF as a
marker on a per-severity layer, so a reviewer opening the drawing in a CAD
viewer sees *where* the mismatches are without opening the report.

Layers
------
============================ ========= =========================================
layer                        ACI       meaning
============================ ========= =========================================
``QA|MISMATCH_CRITICAL``     1 (red)   a critical mismatch
``QA|MISMATCH_WARNING``      30 (orange) a warning
``QA|MISMATCH_INFO``         8 (grey)  an informational finding
``QA|MISMATCH_REF``          4 (cyan)  ghost outlines of the two items involved
============================ ========= =========================================

The drawing is **idempotent**: every entity on a ``QA|MISMATCH*`` layer is
deleted before the new ones are drawn, so re-running the stage never stacks
markers.

Units.  A finding's ``location`` and ``offset_mm`` are in **MECH world
drawing units** and **millimetres** respectively; the marker radius and the
arrow are specified in millimetres, so everything is converted through
``mm_per_unit`` before it reaches the DXF.
"""
from __future__ import annotations

import math
from pathlib import Path

__all__ = ["draw", "draw_into", "MISMATCH_CRITICAL", "MISMATCH_WARNING",
           "MISMATCH_INFO", "MISMATCH_REF", "XDATA_APPID"]

MISMATCH_CRITICAL = "QA|MISMATCH_CRITICAL"     # ACI 1
MISMATCH_WARNING = "QA|MISMATCH_WARNING"       # ACI 30
MISMATCH_INFO = "QA|MISMATCH_INFO"             # ACI 8
MISMATCH_REF = "QA|MISMATCH_REF"               # ACI 4

#: The XDATA application id the markers carry their id / uid / kind in.
XDATA_APPID = "OVERLAPQA"

_SEVERITY_LAYER = {
    "CRITICAL": MISMATCH_CRITICAL,
    "WARNING": MISMATCH_WARNING,
    "INFO": MISMATCH_INFO,
}

_MIN_RADIUS_MM = 50.0        # the "auto" floor
_RADIUS_REL = 0.03           # × the median grid spacing
_TEXT_REL = 0.4              # text height, × the marker radius
_ARROWHEAD_DEG = 25.0
_MAX_ARROW_K = 1000


def _ensure_layer(doc, name: str, color: int):
    """The layer table entry ``name``, created with ``color`` if needed."""
    layer = doc.layers.get(name) if doc.layers.has_entry(name) else None
    if layer is None:
        layer = doc.layers.new(name)
    layer.dxf.color = int(color)
    if layer.dxf.hasattr("true_color"):
        del layer.dxf.true_color
    return layer


def _clear(doc) -> int:
    """Delete every entity on a ``QA|MISMATCH*`` layer.  Returns the count."""
    removed = 0
    for layout in [doc.modelspace()] + [b for b in doc.blocks
                                        if not b.name.startswith("*")]:
        for e in list(layout):
            try:
                if e.dxf.layer.startswith("QA|MISMATCH"):
                    layout.delete_entity(e)
                    removed += 1
            except Exception:
                continue
    return removed


def _radius_units(marker_radius_mm, median_spacing_units, mm_per_unit) -> float:
    """The marker radius, in drawing units."""
    if marker_radius_mm not in (None, "", "auto"):
        return float(marker_radius_mm) / max(mm_per_unit, 1e-9)
    floor = _MIN_RADIUS_MM / max(mm_per_unit, 1e-9)
    return max(floor, _RADIUS_REL * float(median_spacing_units or 0.0))


def _arrow_k(offset_len: float, r: float) -> int:
    """How far to stretch the offset arrow so it is visible."""
    if offset_len <= 0.0:
        return 1
    half = 0.5 * r
    if offset_len >= half:
        return 1
    return min(_MAX_ARROW_K, max(1, int(math.ceil(half / offset_len))))


def _arrow(msp, layer, x, y, ux, uy, length, r):
    """A shaft from (x, y) along (ux, uy) plus two arrowhead lines."""
    ex, ey = x + ux * length, y + uy * length
    msp.add_line((x, y), (ex, ey), dxfattribs={"layer": layer})
    ang = math.atan2(uy, ux)
    for sign in (1.0, -1.0):
        a = ang + sign * math.radians(_ARROWHEAD_DEG)
        hx, hy = ex - math.cos(a) * r * 0.5, ey - math.sin(a) * r * 0.5
        msp.add_line((ex, ey), (hx, hy), dxfattribs={"layer": layer})
    return 3


def _ghost(msp, layer, x, y, size_mm, mm_per_unit):
    """A ghost rectangle for one of the two items involved."""
    w = float((size_mm or {}).get("w", 0.0)) / max(mm_per_unit, 1e-9)
    h = float((size_mm or {}).get("h", 0.0)) / max(mm_per_unit, 1e-9)
    if w <= 0.0 or h <= 0.0:
        return
    hw, hh = w / 2.0, h / 2.0
    pts = [(x - hw, y - hh), (x + hw, y - hh), (x + hw, y + hh), (x - hw, y + hh)]
    for i in range(4):
        msp.add_line(pts[i], pts[(i + 1) % 4], dxfattribs={"layer": layer})


def draw(doc, findings, *, mm_per_unit: float = 1.0,
         marker_radius_mm="auto", median_spacing_units=None,
         draw_info: bool = False, draw_refs: bool = True) -> dict:
    """Draw every finding into ``doc``.  Returns a small summary dict."""
    mm_per_unit = float(mm_per_unit) or 1.0
    _ensure_layer(doc, MISMATCH_CRITICAL, 1)
    _ensure_layer(doc, MISMATCH_WARNING, 30)
    _ensure_layer(doc, MISMATCH_INFO, 8)
    _ensure_layer(doc, MISMATCH_REF, 4)
    try:
        if not doc.appids.has_entry(XDATA_APPID):
            doc.appids.add(XDATA_APPID)
    except Exception:
        pass

    removed = _clear(doc)
    msp = doc.modelspace()
    r = _radius_units(marker_radius_mm, median_spacing_units, mm_per_unit)
    h = _TEXT_REL * r

    drawn = 0
    for f in findings:
        sev = getattr(f, "severity", "INFO")
        if sev == "INFO" and not draw_info:
            continue
        layer = _SEVERITY_LAYER.get(sev, MISMATCH_INFO)
        x, y = (float(f.location[0]), float(f.location[1]))
        kind = getattr(f, "kind", "")
        fid = getattr(f, "id", "") or ""
        fuid = getattr(f, "uid", "") or ""

        # the marker itself
        circle = msp.add_circle((x, y), r, dxfattribs={"layer": layer})
        try:
            circle.set_xdata(XDATA_APPID, [(1000, fid), (1000, fuid),
                                           (1000, kind)])
        except Exception:
            pass

        # the offset arrow
        off = getattr(f, "offset_mm", None)
        if off:
            ox, oy = float(off[0]) / mm_per_unit, float(off[1]) / mm_per_unit
            olen = math.hypot(ox, oy)
            if olen > 0.0:
                ux, uy = ox / olen, oy / olen
                k = _arrow_k(olen, r)
                _arrow(msp, layer, x, y, ux, uy, olen * k, r)

        # the label
        mag = getattr(f, "magnitude_mm", None)
        text = f"{fid} {kind}"
        if mag is not None:
            text += f" {float(mag):.1f}mm"
        if off:
            olen = math.hypot(float(off[0]), float(off[1])) / mm_per_unit
            k = _arrow_k(olen, r)
            if k > 1:
                text += f" (x{k})"
        msp.add_text(text, dxfattribs={"insert": (x + r, y + r),
                                       "height": h, "layer": layer})

        # a rotation arc (PATTERN_ROTATED)
        rot = getattr(f, "rotation_deg", None)
        if kind == "PATTERN_ROTATED" and rot:
            arc_r = r * 2.0
            start = 0.0
            end = float(rot) % 360.0
            if end < start:
                end += 360.0
            msp.add_arc((x, y), arc_r, start, end, dxfattribs={"layer": layer})

        # the cluster circle (SYSTEMATIC_OFFSET)
        if kind == "SYSTEMATIC_OFFSET":
            bbox = (getattr(f, "details", None) or {}).get("bbox")
            if bbox and len(bbox) == 4:
                x0, y0, x1, y1 = (float(v) for v in bbox)
                cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
                rad = math.hypot(x1 - x0, y1 - y0) / 2.0
                if rad > 0.0:
                    msp.add_circle((cx, cy), rad, dxfattribs={"layer": layer})

        # ghost outlines of the two items involved
        if draw_refs:
            ref = getattr(f, "mech_ref", None)
            if ref is not None:
                _ghost(msp, MISMATCH_REF, x, y,
                       getattr(ref, "size_mm", None) or {}, mm_per_unit)
            sref = getattr(f, "str_ref", None)
            if sref is not None:
                loc = getattr(f, "str_location", None)
                if loc:
                    _ghost(msp, MISMATCH_REF, float(loc[0]), float(loc[1]),
                           getattr(sref, "size_mm", None) or {}, mm_per_unit)

        drawn += 1

    return {"drawn": drawn, "removed": removed, "radius_units": r}


def draw_into(root: Path, result, *, policy=None) -> dict:
    """Draw ``result``'s findings into ``<root>/out/superimposed.dxf`` in place.

    The DXF is re-read and re-saved; the drawing itself is untouched apart
    from the added ``QA|MISMATCH*`` entities.
    """
    from core import dxfio

    dxf_path = Path(root) / "out" / "superimposed.dxf"
    if not dxf_path.exists():
        return {"drawn": 0, "removed": 0, "skipped": "no superimposed.dxf"}

    render_cfg = getattr(policy, "render", None) if policy is not None else None
    marker_radius = getattr(render_cfg, "marker_radius_mm", "auto") \
        if render_cfg is not None else "auto"
    draw_info = bool(getattr(render_cfg, "draw_info", False)) \
        if render_cfg is not None else False
    draw_refs = bool(getattr(render_cfg, "draw_refs", True)) \
        if render_cfg is not None else True

    median_spacing = None
    transform_path = Path(root) / "transform.json"
    if transform_path.exists():
        import json
        try:
            median_spacing = json.loads(transform_path.read_text()).get(
                "median_spacing")
        except Exception:
            median_spacing = None

    doc = dxfio.read_internal(str(dxf_path))
    summary = draw(doc, list(getattr(result, "findings", []) or []),
                   mm_per_unit=float(getattr(result, "mm_per_unit", 1.0) or 1.0),
                   marker_radius_mm=marker_radius,
                   median_spacing_units=median_spacing,
                   draw_info=draw_info, draw_refs=draw_refs)
    dxfio.save(doc, str(dxf_path))
    return summary
