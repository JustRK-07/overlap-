#!/usr/bin/env python3
"""core/mech_intra.py — Clashes within the MECH drawing only.

Three independent detectors, all driven from the same closed-polyline
footprints on equipment-bearing layers (MAIN OBJECT, EQUIPMENT,
VESSEL, etc.):

  1. EQUIPMENT_OVERLAP    — two equipment footprints intersect
                              (physically impossible to install both)
  2. BUILDING_CLASH       — equipment vertex outside the building outline
                              (equipment sticks through a wall/outside room)
  3. CLEARANCE_VIOLATION  — gap between two equipment footprints < min_clearance
                              (not enough maintenance access)

Output:
  jobs/<id>/stages/mech_intra.json
    { issues: [{id, type, severity, mech_x, mech_y, ...}, ...],
      stats: {footprints_total, equipment_pairs_checked, ...} }
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import ezdxf

from jobs import job_dir
from .geom import point_segment_distance as _point_segment_distance
from .geom import polygon_centroid as _polygon_centroid
from .geom import segments_intersect as _segments_intersect


# ─────────────────────────────────────────────────────────────────────────────
# Issue schema
# ─────────────────────────────────────────────────────────────────────────────

ISSUE_TYPES = (
    "EQUIPMENT_OVERLAP",   # CRITICAL — two footprints intersect
    "BUILDING_CLASH",      # CRITICAL — equipment outside building outline
    "CLEARANCE_VIOLATION", # WARNING — gap < min_clearance
)

SEVERITY = {
    "EQUIPMENT_OVERLAP": "CRITICAL",
    "BUILDING_CLASH": "CRITICAL",
    "CLEARANCE_VIOLATION": "WARNING",
}

DEFAULT_MIN_CLEARANCE = 900.0   # mm — minimum gap between equipment footprints
# Per API 650 small-tank spacing and typical plant practice for maintenance
# access (operators need to reach valves/instruments on adjacent equipment).

# Layers that almost always carry equipment footprints.
# Anything matching one of these (case-insensitive substring) is treated
# as an equipment footprint candidate.
EQUIPMENT_LAYER_PATTERNS = (
    "MAIN OBJECT",
    "EQUIPMENT", "VESSEL", "PUMP", "TANK", "EXCHANGER",
    "equipment", "vessel", "pump", "tank", "exchanger",
    "CHUTE", "BELT", "HANDRAIL",
)

# Layer patterns that look like an enclosing building / room outline.
# Building outline = very large closed polyline (bbox > 10m on each side).
BUILDING_LAYER_PATTERNS = (
    "BUILDING", "ROOM", "WALL", "BOUNDARY", "civil",
    "Building", "Room", "Wall", "Boundary",
    "CIVIL", "civil",
    "FLOOR", "floor",
    "ENCLOSURE", "enclosure",
)


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Issue:
    """One MECH-intra finding."""
    id: str                                   # e.g. "MECH-001"
    type: str                                 # see ISSUE_TYPES
    severity: str                             # CRITICAL / WARNING / INFO
    # Location (world coords, MECH drawing space)
    mech_x: float
    mech_y: float
    # MECH entities involved
    mech_block: str = ""                      # block name (or "(modelspace)")
    mech_entity: str = ""                     # DXF handle of footprint
    mech_block_b: str = ""                    # second footprint for overlap/clearance
    mech_entity_b: str = ""
    distance: float = 0.0                     # overlap area (mm²) or gap (mm)
    note: str = ""


@dataclass
class MechIntraResult:
    footprints_total: int = 0
    pairs_checked: int = 0
    building_found: bool = False
    enabled_checks: list[str] = field(default_factory=list)
    min_clearance: float = DEFAULT_MIN_CLEARANCE
    issues: list[Issue] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def _bbox(pts: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_overlap(a, b) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _point_in_polygon(px: float, py: float, poly: list[tuple[float, float]]) -> bool:
    """Ray-casting test. Works for any simple polygon."""
    n = len(poly)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and \
           (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _polygons_intersect(a: list, b: list) -> bool:
    """True if any point of A is inside B, any edge crosses, or bbox overlaps.

    Works for any simple polygon (convex or non-convex).
    """
    if len(a) < 3 or len(b) < 3:
        return False
    # Quick bbox reject
    if not _bbox_overlap(_bbox(a), _bbox(b)):
        return False
    # Vertex-in-other test
    for p in a:
        if _point_in_polygon(p[0], p[1], b):
            return True
    for p in b:
        if _point_in_polygon(p[0], p[1], a):
            return True
    # Edge-edge intersection
    for i in range(len(a)):
        a1 = a[i]
        a2 = a[(i + 1) % len(a)]
        for j in range(len(b)):
            b1 = b[j]
            b2 = b[(j + 1) % len(b)]
            if _segments_intersect(a1, a2, b1, b2):
                return True
    return False


def _polygon_distance(a: list, b: list) -> float:
    """Minimum distance between two polygons. 0 if they touch/intersect."""
    if _polygons_intersect(a, b):
        return 0.0
    best = float("inf")
    for i in range(len(a)):
        x1, y1 = a[i]
        x2, y2 = a[(i + 1) % len(a)]
        for px, py in b:
            best = min(best, _point_segment_distance(px, py, x1, y1, x2, y2))
    for i in range(len(b)):
        x1, y1 = b[i]
        x2, y2 = b[(i + 1) % len(b)]
        for px, py in a:
            best = min(best, _point_segment_distance(px, py, x1, y1, x2, y2))
    return best


# ─────────────────────────────────────────────────────────────────────────────
# Footprint extraction
# ─────────────────────────────────────────────────────────────────────────────

def _is_equipment_layer(layer: str) -> bool:
    upper = (layer or "").upper()
    return any(p.upper() in upper for p in EQUIPMENT_LAYER_PATTERNS)


def _is_building_layer(layer: str) -> bool:
    upper = (layer or "").upper()
    return any(p.upper() in upper for p in BUILDING_LAYER_PATTERNS)


def _find_mech_footprints(doc) -> list[dict]:
    """Return list of {pts, block, handle, layer, bbox_area}.

    Pulls closed LWPOLYLINE from modelspace AND from inserted blocks on
    equipment layers. Skips tiny/huge outliers (likely not real equipment).
    """
    out = []
    msp = doc.modelspace()

    # 1) Modelspace closed LWPOLYLINE on equipment layers
    for e in list(msp):
        if e.dxftype() != "LWPOLYLINE" or not getattr(e, "closed", False):
            continue
        layer = e.dxf.layer if e.dxf.hasattr("layer") else ""
        if not _is_equipment_layer(layer):
            continue
        try:
            pts = [(v[0], v[1]) for v in e.vertices()]
        except Exception:
            continue
        if len(pts) < 3:
            continue
        bb = _bbox(pts)
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        # Skip very small (noise) or very large (>10m) — those are room outlines
        if w < 200 or h < 200 or w > 10000 or h > 10000:
            continue
        out.append({
            "pts": pts,
            "block": "(modelspace)",
            "handle": e.dxf.handle if e.dxf.hasattr("handle") else "",
            "layer": layer,
            "bbox_area": w * h,
            "centroid": _polygon_centroid(pts),
        })

    # 2) INSERTs on equipment layers whose block def contains a small rectangle
    for ins in list(msp):
        if ins.dxftype() != "INSERT":
            continue
        ins_layer = ins.dxf.layer if ins.dxf.hasattr("layer") else ""
        if not _is_equipment_layer(ins_layer):
            continue
        try:
            ix, iy = ins.dxf.insert[0], ins.dxf.insert[1]
            sx = ins.dxf.xscale or 1.0
            sy = ins.dxf.yscale if ins.dxf.hasattr("yscale") and ins.dxf.yscale else sx
        except Exception:
            continue
        block = None
        for b in doc.blocks:
            if b.name == ins.dxf.name:
                block = b
                break
        if block is None:
            continue
        for e in block:
            if e.dxftype() != "LWPOLYLINE" or not getattr(e, "closed", False):
                continue
            try:
                pts_local = [(v[0], v[1]) for v in e.vertices()]
            except Exception:
                continue
            if len(pts_local) < 3:
                continue
            xs = [p[0] for p in pts_local]
            ys = [p[1] for p in pts_local]
            w = (max(xs) - min(xs)) * abs(sx)
            h = (max(ys) - min(ys)) * abs(sy)
            if w < 200 or h < 200 or w > 10000 or h > 10000:
                continue
            pts_world = [(ix + sx * p[0], iy + sy * p[1]) for p in pts_local]
            out.append({
                "pts": pts_world,
                "block": ins.dxf.name,
                "handle": e.dxf.handle if e.dxf.hasattr("handle") else "",
                "layer": ins_layer,
                "bbox_area": w * h,
                "centroid": _polygon_centroid(pts_world),
            })
    return out


def _find_building_outline(doc) -> list | None:
    """Find the largest closed LWPOLYLINE on a building-layer.

    Returns the polygon pts (or None if no plausible building outline).
    """
    msp = doc.modelspace()
    candidates = []
    for e in list(msp):
        if e.dxftype() != "LWPOLYLINE" or not getattr(e, "closed", False):
            continue
        layer = e.dxf.layer if e.dxf.hasattr("layer") else ""
        if not _is_building_layer(layer):
            continue
        try:
            pts = [(v[0], v[1]) for v in e.vertices()]
        except Exception:
            continue
        if len(pts) < 4:
            continue
        bb = _bbox(pts)
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        # Building outlines are big (typically >5m on each side)
        if w > 5000 and h > 5000:
            candidates.append((w * h, pts, layer))
    if not candidates:
        return None
    # Pick the largest one
    candidates.sort(key=lambda c: -c[0])
    return candidates[0][1]


# ─────────────────────────────────────────────────────────────────────────────
# Detectors
# ─────────────────────────────────────────────────────────────────────────────

def _detect_overlaps(footprints: list[dict]) -> list[Issue]:
    """Pairwise EQUIPMENT_OVERLAP detection."""
    issues = []
    n = len(footprints)
    for i in range(n):
        for j in range(i + 1, n):
            a = footprints[i]
            b = footprints[j]
            if _polygons_intersect(a["pts"], b["pts"]):
                cx = (a["centroid"][0] + b["centroid"][0]) / 2
                cy = (a["centroid"][1] + b["centroid"][1]) / 2
                # Distance between centroids as the "distance" field (smaller = closer)
                dx = a["centroid"][0] - b["centroid"][0]
                dy = a["centroid"][1] - b["centroid"][1]
                dist = math.hypot(dx, dy)
                issues.append(Issue(
                    id="",                       # filled in by run()
                    type="EQUIPMENT_OVERLAP",
                    severity="CRITICAL",
                    mech_x=cx, mech_y=cy,
                    mech_block=a["block"],
                    mech_entity=a["handle"],
                    mech_block_b=b["block"],
                    mech_entity_b=b["handle"],
                    distance=dist,
                    note=f"Footprints intersect ({a['block']} ↔ {b['block']})",
                ))
    return issues


def _detect_building_clash(footprints: list[dict], building: list | None) -> list[Issue]:
    """Footprint vertices outside the building outline."""
    if building is None:
        return []
    issues = []
    # Building is a simple polygon — point-in-polygon test
    for fp in footprints:
        outside_verts = [p for p in fp["pts"] if not _point_in_polygon(p[0], p[1], building)]
        if outside_verts:
            # Centroid of outside verts
            cx = sum(p[0] for p in outside_verts) / len(outside_verts)
            cy = sum(p[1] for p in outside_verts) / len(outside_verts)
            issues.append(Issue(
                id="",
                type="BUILDING_CLASH",
                severity="CRITICAL",
                mech_x=cx, mech_y=cy,
                mech_block=fp["block"],
                mech_entity=fp["handle"],
                mech_block_b="",
                mech_entity_b="",
                distance=float(len(outside_verts)),
                note=f"{len(outside_verts)} vertex(es) outside building outline",
            ))
    return issues


def _detect_clearance(footprints: list[dict], min_clearance: float) -> list[Issue]:
    """Pairwise clearance check — gap < min_clearance triggers WARNING."""
    issues = []
    n = len(footprints)
    for i in range(n):
        for j in range(i + 1, n):
            a = footprints[i]
            b = footprints[j]
            d = _polygon_distance(a["pts"], b["pts"])
            if 0 < d < min_clearance:
                cx = (a["centroid"][0] + b["centroid"][0]) / 2
                cy = (a["centroid"][1] + b["centroid"][1]) / 2
                issues.append(Issue(
                    id="",
                    type="CLEARANCE_VIOLATION",
                    severity="WARNING",
                    mech_x=cx, mech_y=cy,
                    mech_block=a["block"],
                    mech_entity=a["handle"],
                    mech_block_b=b["block"],
                    mech_entity_b=b["handle"],
                    distance=d,
                    note=f"Gap {d:.0f}mm < {min_clearance:.0f}mm minimum",
                ))
    return issues


# ─────────────────────────────────────────────────────────────────────────────
# Public entrypoint
# ─────────────────────────────────────────────────────────────────────────────

def run(job_id: str, *,
        enabled_checks: list[str] | None = None,
        min_clearance: float = 900.0,
        max_issues: int = 500,
        pair_dir = None) -> MechIntraResult:
    """Stage 7.5a: detect clashes within the MECH drawing only.

    enabled_checks: subset of {EQUIPMENT_OVERLAP, BUILDING_CLASH, CLEARANCE_VIOLATION}.
    None means run all.

    `pair_dir` is the per-pair cleaned dir for multi-pair runs.
    When None, the legacy single-job path is taken — bit-equivalent
    behaviour.

    max_issues caps the total count to keep DXF rendering fast.
    """
    if enabled_checks is None:
        enabled_checks = list(ISSUE_TYPES)

    d = job_dir(job_id)
    if pair_dir is not None:
        root = Path(pair_dir).parent
        cleaned = sorted(Path(pair_dir).glob("*_clean.dxf"))
    else:
        root = d
        cleaned = sorted((d / "cleaned").glob("*_clean.dxf"))
    # MECH is alphabetically first
    mech_path = cleaned[0] if cleaned else None
    if mech_path is None or not mech_path.exists():
        raise RuntimeError("MECH cleaned DXF missing")

    doc = ezdxf.readfile(str(mech_path))
    footprints = _find_mech_footprints(doc)
    building = _find_building_outline(doc)

    result = MechIntraResult(
        footprints_total=len(footprints),
        pairs_checked=len(footprints) * (len(footprints) - 1) // 2,
        building_found=building is not None,
        enabled_checks=list(enabled_checks),
        min_clearance=min_clearance,
    )

    all_issues: list[Issue] = []

    if "EQUIPMENT_OVERLAP" in enabled_checks:
        all_issues.extend(_detect_overlaps(footprints))
    if "BUILDING_CLASH" in enabled_checks:
        all_issues.extend(_detect_building_clash(footprints, building))
    if "CLEARANCE_VIOLATION" in enabled_checks:
        all_issues.extend(_detect_clearance(footprints, min_clearance))

    truncated = False
    if len(all_issues) > max_issues:
        sev_rank = {"CRITICAL": 0, "WARNING": 1, "INFO": 2, "": 3}
        all_issues.sort(key=lambda i: (sev_rank.get(i.severity, 9), i.distance))
        all_issues = all_issues[:max_issues]
        truncated = True

    # Assign sequential IDs
    for idx, iss in enumerate(all_issues, start=1):
        iss.id = f"MECH-{idx:03d}"
    result.issues = all_issues

    # Persist
    out = root / "stages" / "mech_intra.json"
    out.write_text(json.dumps({
        "footprints_total": result.footprints_total,
        "pairs_checked": result.pairs_checked,
        "building_found": result.building_found,
        "enabled_checks": result.enabled_checks,
        "min_clearance": result.min_clearance,
        "max_issues": max_issues,
        "truncated": truncated,
        "issues": [asdict(i) for i in result.issues],
    }, indent=2))

    return result
