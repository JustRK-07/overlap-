#!/usr/bin/env python3
"""core/spotcheck.py — Support verification between MECH and STR.

Auto-detects MECH equipment support points (saddles / baseplates /
bolted feet), then checks whether each one is structurally supported
by the STR drawing.

Issue types detected:
  1. NO_BEAM_SUPPORT   — saddle centroid not over any STR beam within
                         `beam_tolerance`. Most critical.
  2. COLUMN_CLASH      — saddle footprint overlaps a STR column.
                         Equipment physically cannot fit.
  3. BAY_OVERHANG      — saddle or equipment footprint extends beyond
                         the structural bay boundary.
  4. GRID_MISALIGN     — saddle centroid offset from nearest STR gridline
                         exceeds `grid_tolerance`.

The output is a list of `Issue` dicts ready to be drawn on the
superimposed DXF as cloud callouts and dumped into a JSON / DOCX report.

Reads:
  jobs/<id>/out/superimposed.dxf       (the overlay result)
  jobs/<id>/transform.json             (the fitted affine)
  jobs/<id>/cleaned/*_clean.dxf        (raw cleaned inputs)

Writes:
  jobs/<id>/stages/spotcheck.json      (raw issues for inspection)
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import ezdxf

from jobs import job_dir
from .geom import point_segment_distance as _distance_point_to_segment
from .geom import polygon_centroid as _polygon_centroid
from .smart3d import detect_anchor_pattern as _detect_anchor_pattern
from .smart3d import segment_world_coords as _segment_world_coords
from .smart3d import walk_block_lines as _walk_block_lines


# ─────────────────────────────────────────────────────────────────────────────
# Issue schema
# ─────────────────────────────────────────────────────────────────────────────

ISSUE_TYPES = (
    # MECH ↔ STR (cross-drawing)
    "NO_BEAM_SUPPORT",     # saddle centroid not over a beam
    "COLUMN_CLASH",        # equipment footprint hits a column
    "BAY_OVERHANG",        # saddle outside STR bay boundary
    "GRID_MISALIGN",       # saddle centroid off-grid by too much
    # MECH intra
    "EQUIPMENT_OVERLAP",   # two equipment footprints intersect
    "BUILDING_CLASH",      # equipment extends outside building outline
    "CLEARANCE_VIOLATION", # gap < min_clearance between equipment
    # STR intra
    "BEAM_COLUMN_CLASH",   # beam within tolerance of a column
    "BEAM_BEAM_CROSSING",  # two beams cross in plan
)

SEVERITY = {
    "COLUMN_CLASH": "CRITICAL",
    "NO_BEAM_SUPPORT": "CRITICAL",
    "BAY_OVERHANG": "WARNING",
    "GRID_MISALIGN": "INFO",
    "EQUIPMENT_OVERLAP": "CRITICAL",
    "BUILDING_CLASH": "CRITICAL",
    "CLEARANCE_VIOLATION": "WARNING",
    "BEAM_COLUMN_CLASH": "CRITICAL",
    "BEAM_BEAM_CROSSING": "WARNING",
}


@dataclass
class Issue:
    """One spot-check finding. Union of all cross-drawing + intra issues."""
    id: str                                   # e.g. "ISSUE-001" / "MECH-001" / "STR-001"
    type: str                                 # see ISSUE_TYPES
    severity: str                             # CRITICAL / WARNING / INFO
    # Location of the issue (world coords)
    mech_x: float
    mech_y: float
    # MECH side (set for cross-drawing + MECH intra)
    mech_block: str = ""                      # INSERT block name, or "(modelspace)"
    mech_entity: str = ""                     # DXF handle
    mech_block_b: str = ""                    # second MECH entity (MECH intra pair)
    mech_entity_b: str = ""
    # STR side (set for cross-drawing + STR intra)
    str_block: str = ""                       # STR block that should support it
    str_entity: str = ""                      # DXF handle of beam/column
    str_block_b: str = ""                     # second STR entity (STR intra pair)
    str_entity_b: str = ""
    distance: float = 0.0                     # distance / overlap area
    note: str = ""                            # short human-readable note


@dataclass
class SpotCheckResult:
    """All findings for a single job."""
    saddles_total: int = 0
    saddles_supported: int = 0
    issues: list[Issue] = field(default_factory=list)
    tolerance_beam: float = 200.0             # mm — saddle must be over a beam within this radius
    tolerance_grid: float = 50.0              # mm — saddle centroid vs nearest gridline
    tolerance_clash: float = 0.0              # mm — saddle footprint must not touch a column


# ─────────────────────────────────────────────────────────────────────────────
# MECH saddle detection
# ─────────────────────────────────────────────────────────────────────────────

# MECH layers that almost always indicate structural support geometry:
# saddles, baseplates, bolted feet, equipment supports.
SADDLE_LAYER_PATTERNS = (
    "MAIN OBJECT",          # primary equipment body — saddles live here too
    "support", "saddle", "baseplate", "foot", "skid",
    "support", "SADDLE", "BASEPLATE", "FOOT", "SKID",
)

# Heuristic for a "saddle-ish" rectangle: small, axis-aligned,
# 4-vertex closed LWPOLYLINE on one of the saddle layers, in a
# reasonable size range (say 200mm..5000mm wide).
def _find_mech_saddles(doc) -> list[dict]:
    """Return a list of {centroid: (x,y), block_name, handle, layer, footprint}."""
    saddles = []
    msp = doc.modelspace()

    def _is_saddle_layer(layer: str) -> bool:
        upper = (layer or "").upper()
        return any(p.upper() in upper for p in SADDLE_LAYER_PATTERNS)

    # 1) Standalone LWPOLYLINE on a saddle layer — treat as baseplate outline
    for e in list(msp):
        if e.dxftype() != "LWPOLYLINE":
            continue
        if not e.closed:
            continue
        layer = e.dxf.layer if e.dxf.hasattr("layer") else ""
        if not _is_saddle_layer(layer):
            continue
        try:
            pts = [(v[0], v[1]) for v in e.vertices()]
        except Exception:
            continue
        if len(pts) < 3:
            continue
        # Bounding-box check: skip huge outlines (more likely the equipment body itself)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        if not (200 <= w <= 5000 and 200 <= h <= 5000):
            continue
        cx, cy = _polygon_centroid(pts)
        saddles.append({
            "centroid": (cx, cy),
            "block_name": "(modelspace)",
            "handle": e.dxf.handle if e.dxf.hasattr("handle") else "",
            "layer": layer,
            "footprint": pts,
        })

    # 2) INSERT blocks on saddle layers whose block def contains a small
    #    rectangular LWPOLYLINE — saddles often come as named block inserts.
    for ins in list(msp):
        if ins.dxftype() != "INSERT":
            continue
        ins_layer = ins.dxf.layer if ins.dxf.hasattr("layer") else ""
        if not _is_saddle_layer(ins_layer):
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
            if not (200 <= w <= 5000 and 200 <= h <= 5000):
                continue
            pts_world = [(ix + sx * p[0], iy + sy * p[1]) for p in pts_local]
            cx, cy = _polygon_centroid(pts_world)
            saddles.append({
                "centroid": (cx, cy),
                "block_name": ins.dxf.name,
                "handle": e.dxf.handle if e.dxf.hasattr("handle") else "",
                "layer": ins_layer,
                "footprint": pts_world,
            })
    return saddles


# ─────────────────────────────────────────────────────────────────────────────
# STR member extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_str_beams_and_columns(doc) -> tuple[list, list]:
    """Walk every non-anonymous block + its INSERTs.

    Skips Bolt-* (their LINEs are bolt outlines, not beams — positions
    extracted separately via _extract_str_bolts) and Connection-*
    (empty placeholder blocks).

    Detects Smart3D-style "anchor pattern" (all INSERTs share same insert+
    scale — block LINE coords are already world coords). In that case,
    skip the insert+scale transform to avoid squishing all members onto
    a single anchor point.

    Returns:
      beams:    list of ((x1,y1),(x2,y2), handle, block_name)
      columns:  list of ((x1,y1),(x2,y2), handle, block_name)
                (long-axis = vertical, short-axis = horizontal)
    """
    is_anchor, _, _, _, _ = _detect_anchor_pattern(doc)
    apply_t = not is_anchor

    beams = []
    columns = []
    for ins in doc.modelspace():
        if ins.dxftype() != "INSERT":
            continue
        block_name = ins.dxf.name
        # Skip non-structural blocks: axis labels, bolts, empty placeholders
        if (block_name.startswith("GridLine-")
                or block_name.startswith(" Line-")
                or block_name.startswith("Bolt-")
                or block_name.startswith("Connection-")):
            continue
        block = None
        for b in doc.blocks:
            if b.name == block_name:
                block = b
                break
        if block is None:
            continue
        if apply_t:
            ins_pos, ins_scale = _segment_world_coords(ins)
        else:
            ins_pos, ins_scale = (0.0, 0.0), (1.0, 1.0)
        for (a, b) in _walk_block_lines(block, ins_pos, ins_scale,
                                         apply_transform=apply_t):
            dx = b[0] - a[0]
            dy = b[1] - a[1]
            length = math.hypot(dx, dy)
            if length < 100:
                continue
            try:
                handle = ins.dxf.handle if ins.dxf.hasattr("handle") else ""
            except Exception:
                handle = ""
            # Heuristic: horizontal member (|dy| < 0.1 * length) is a beam,
            # vertical member (|dx| < 0.1 * length) is a column.
            if abs(dy) < 0.1 * length:
                beams.append((a, b, handle, block_name))
            elif abs(dx) < 0.1 * length:
                columns.append((a, b, handle, block_name))
            else:
                # Diagonal — count as beam (rafter, brace). Most MECH support
                # checks are against horizontal beams anyway, but include these
                # so we don't miss anything.
                beams.append((a, b, handle, block_name))
    return beams, columns


def _extract_str_bolts(doc) -> list[tuple[float, float]]:
    """Walk every Bolt-* INSERT and return its world-space position.

    A bolt's "location" is the centroid of all LINE endpoints inside its
    block. For Smart3D-style files (all INSERTs share the same anchor),
    block-local coords ARE world coords and we use them directly.
    For normal DXFs, we transform by insert + xscale/yscale.

    Returns list of (x, y) tuples in STR coords. Used by run() to check
    whether MECH saddles are bolted to the structure (within beam_tolerance
    of a bolt = supported).
    """
    is_anchor, _, _, _, _ = _detect_anchor_pattern(doc)
    apply_t = not is_anchor

    bolt_positions = []
    for ins in doc.modelspace():
        if ins.dxftype() != "INSERT":
            continue
        if not ins.dxf.name.startswith("Bolt-"):
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
        xs, ys = [], []
        for e in block:
            if e.dxftype() != "LINE":
                continue
            try:
                s, end = e.dxf.start, e.dxf.end
                if apply_t:
                    xs.extend([ix + sx * s[0], ix + sx * end[0]])
                    ys.extend([iy + sy * s[1], iy + sy * end[1]])
                else:
                    xs.extend([s[0], end[0]])
                    ys.extend([s[1], end[1]])
            except Exception:
                continue
        if xs:
            bolt_positions.append((sum(xs) / len(xs), sum(ys) / len(ys)))
    return bolt_positions


def _extract_str_gridlines(doc) -> list[tuple[float, str]]:
    """Walk every GridLine-* INSERT; return (axis_coord, 'X'|'Y')."""
    coords = []
    for ins in doc.modelspace():
        if ins.dxftype() != "INSERT":
            continue
        nm = ins.dxf.name
        if not (nm.startswith("GridLine-") or nm.startswith(" Line-")):
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
            if e.dxftype() != "LINE":
                continue
            try:
                s, end = e.dxf.start, e.dxf.end
            except Exception:
                continue
            x1 = ix + sx * s[0]; y1 = iy + sy * s[1]
            x2 = ix + sx * end[0]; y2 = iy + sy * end[1]
            dx = x2 - x1
            dy = y2 - y1
            length = math.hypot(dx, dy)
            if length < 100:
                continue
            if abs(dx) < 0.1 * length:    # vertical → X gridline
                coords.append((x1, "X"))
            elif abs(dy) < 0.1 * length:  # horizontal → Y gridline
                coords.append((y1, "Y"))
    return coords


def _compute_bay_bounds(gridlines: list[tuple[float, str]]) -> tuple[float, float, float, float] | None:
    """From (coord, axis) pairs, compute (xmin, ymin, xmax, ymax) of STR bay area."""
    xs = sorted(c for c, ax in gridlines if ax == "X")
    ys = sorted(c for c, ax in gridlines if ax == "Y")
    if not xs or not ys:
        return None
    return (xs[0], ys[0], xs[-1], ys[-1])


# ─────────────────────────────────────────────────────────────────────────────
# Issue detection
# ─────────────────────────────────────────────────────────────────────────────

def _nearest_beam_distance(px, py, beams) -> tuple[float, tuple | None]:
    """Distance from (px,py) to nearest beam segment. Returns (dist, beam)."""
    best = float("inf")
    best_beam = None
    for seg in beams:
        (a, b, handle, name) = seg
        d = _distance_point_to_segment(px, py, a[0], a[1], b[0], b[1])
        if d < best:
            best = d
            best_beam = seg
    return best, best_beam


def _nearest_gridline_distance(px, py, gridlines) -> tuple[float, str]:
    """Distance from (px,py) to nearest gridline coord."""
    best = float("inf")
    best_kind = ""
    for coord, kind in gridlines:
        d = abs(px - coord) if kind == "X" else abs(py - coord)
        if d < best:
            best = d
            best_kind = kind
    return best, best_kind


def _column_collision(footprint: list[tuple[float, float]], columns,
                       tolerance: float) -> tuple | None:
    """If any segment of the footprint comes within `tolerance` of a column, return it."""
    if tolerance <= 0:
        return None
    for seg in columns:
        (a, b, handle, name) = seg
        # Column center is midpoint
        cx = (a[0] + b[0]) / 2
        cy = (a[1] + b[1]) / 2
        # Distance from column center to each footprint segment
        for i in range(len(footprint)):
            x1, y1 = footprint[i]
            x2, y2 = footprint[(i + 1) % len(footprint)]
            d = _distance_point_to_segment(cx, cy, x1, y1, x2, y2)
            if d < tolerance:
                return seg
    return None


def _seg_in_bay(footprint: list[tuple[float, float]],
                bay: tuple[float, float, float, float]) -> bool:
    """False if any vertex is outside the bay."""
    xmin, ymin, xmax, ymax = bay
    for x, y in footprint:
        if x < xmin or x > xmax or y < ymin or y > ymax:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Public entrypoint
# ─────────────────────────────────────────────────────────────────────────────

def _affine_to_str(scale_x, scale_y, tx, ty, x, y) -> tuple[float, float]:
    """Map STR coord → MECH coord using fitted transform."""
    return (scale_x * x + tx, scale_y * y + ty)


def _affine_to_mech(scale_x, scale_y, tx, ty, x, y) -> tuple[float, float]:
    """Inverse: MECH coord → STR coord. Solves y = ax*x + bx for x = (y - bx) / ax."""
    if abs(scale_x) < 1e-9:
        return (0.0, 0.0)
    if abs(scale_y) < 1e-9:
        return (0.0, 0.0)
    return ((x - tx) / scale_x, (y - ty) / scale_y)


# Subset of MECH↔STR checks handled by spotcheck itself.
# (MECH intra is handled by core/mech_intra.py, STR intra by core/str_intra.py.)
CROSS_DRAWING_TYPES = (
    "NO_BEAM_SUPPORT",
    "COLUMN_CLASH",
    "BAY_OVERHANG",
    "GRID_MISALIGN",
)


def _cluster_intra(issues: list, *, radius: float) -> list:
    """Group issues of the same type within `radius` of each other.

    For each type separately, walks through issues and merges any whose
    location (mech_x, mech_y) is within `radius` of an existing cluster
    centroid. The representative keeps the most-severe issue's data and
    gets a "(N issues clustered)" suffix in the note.

    Returns the clustered list, sorted by severity then original order.
    """
    if radius <= 0 or not issues:
        return issues

    # Group by type (cluster per type — different issue types don't merge)
    by_type: dict[str, list] = {}
    for i in issues:
        by_type.setdefault(i.type, []).append(i)

    out: list = []
    for type_name, group in by_type.items():
        clusters: list = []   # each: {"centroid": (x,y), "representative": Issue, "count": int}
        for iss in group:
            placed = False
            for cluster in clusters:
                cx, cy = cluster["centroid"]
                d = math.hypot(iss.mech_x - cx, iss.mech_y - cy)
                if d <= radius:
                    # Merge into this cluster
                    n = cluster["count"]
                    # Update centroid (running mean)
                    cluster["centroid"] = (
                        (cx * n + iss.mech_x) / (n + 1),
                        (cy * n + iss.mech_y) / (n + 1),
                    )
                    cluster["count"] = n + 1
                    # Keep the most-severe issue as representative
                    sev_rank = {"CRITICAL": 0, "WARNING": 1, "INFO": 2, "": 3}
                    if sev_rank.get(iss.severity, 9) < sev_rank.get(cluster["representative"].severity, 9):
                        cluster["representative"] = iss
                    placed = True
                    break
            if not placed:
                clusters.append({
                    "centroid": (iss.mech_x, iss.mech_y),
                    "representative": iss,
                    "count": 1,
                })

        # Emit one issue per cluster, with count annotation
        for cluster in clusters:
            rep = cluster["representative"]
            n = cluster["count"]
            if n > 1:
                # Clone so we don't mutate the source
                from dataclasses import replace
                rep = replace(rep,
                              mech_x=cluster["centroid"][0],
                              mech_y=cluster["centroid"][1],
                              note=(rep.note + f"  [cluster: {n} issues within {radius:.0f}mm]").strip())
            out.append(rep)

    return out

# Defaults — used when no enabled_checks list is provided.
ALL_ENABLED_CHECKS = (
    "EQUIPMENT_OVERLAP", "BUILDING_CLASH", "CLEARANCE_VIOLATION",
    "BEAM_COLUMN_CLASH", "BEAM_BEAM_CROSSING",
    "NO_BEAM_SUPPORT", "COLUMN_CLASH", "BAY_OVERHANG", "GRID_MISALIGN",
)


def run(job_id: str, *,
        enabled_checks: list[str] | None = None,
        beam_tolerance: float = 500.0,
        grid_tolerance: float = 300.0,
        column_tolerance: float = 100.0,
        max_issues: int = 500,
        cluster_radius: float = 5000.0,
        pair_dir: Path | None = None) -> SpotCheckResult:
    """Stage 7.5: detect support issues. Returns SpotCheckResult.

    Merges 3 sources into a single issues list:
      1. Cross-drawing (this file)        — saddles vs beams/columns/grid/bay
      2. MECH intra (core/mech_intra.py) — equipment overlap / building / clearance
      3. STR intra  (core/str_intra.py)  — beam↔column / beam↔beam

    `pair_dir` is the per-pair `cleaned/` directory
    (`jobs/<id>/out/pairs/<pair_id>/cleaned/`) for Slice-1 multi-pair
    runs. When None, the legacy single-pair path is taken — bit-equivalent
    behaviour.

    Default tolerances (industry-researched):
      beam_tolerance=500mm     — beam flange width (~300mm) + erection ±10mm
                                 (AISC 303) + saddle placement slack
      column_tolerance=100mm   — AISC 303 erection ±10mm + thermal expansion
                                 + minimum code clearance
      grid_tolerance=300mm     — typical saddle-to-grid alignment tolerance;
                                 tighter than this flags false positives
                                 because grids are reference lines, not
                                 physical support edges

    cluster_radius=5000mm — issues within 5m of each other (same type) get
                            collapsed into one cluster with a count note.
                            Prevents 500 nearly-identical findings.
    max_issues=500         — total cap on the merged list (cross-drawing
                              always preserved; only intra gets capped).

    enabled_checks: subset of ALL_ENABLED_CHECKS. None = run all.
    Tolerance units are millimetres in the MECH drawing's coord system.
    """
    if enabled_checks is None:
        enabled_checks = list(ALL_ENABLED_CHECKS)

    d = job_dir(job_id)
    if pair_dir is not None:
        root = Path(pair_dir).parent
        cleaned = sorted(Path(pair_dir).glob("*_clean.dxf"))
    else:
        root = d
        cleaned = sorted((d / "cleaned").glob("*_clean.dxf"))
    if len(cleaned) != 2:
        raise RuntimeError(f"Need 2 cleaned DXFs; found {len(cleaned)}")

    # The MECH drawing is the one with the equipment — alphabetical order
    # from convert.py places MECH first (drawing_a → TP-104 MECH GA_clean3.dxf).
    mech_path = cleaned[0]
    str_path = cleaned[1]

    # The transform is MECH ← STR (scale_x * STR_x + tx = MECH_x).
    tpath = root / "transform.json"
    if not tpath.exists():
        raise RuntimeError("transform.json missing")
    t = json.loads(tpath.read_text())
    ax, ay, bx, by = t["scale_x"], t["scale_y"], t["tx"], t["ty"]

    mech_doc = ezdxf.readfile(str(mech_path))
    str_doc = ezdxf.readfile(str(str_path))

    saddles = _find_mech_saddles(mech_doc)
    beams, columns = _extract_str_beams_and_columns(str_doc)
    bolts = _extract_str_bolts(str_doc)   # support points (MECH saddles bolt here)
    gridlines = _extract_str_gridlines(str_doc)
    bay = _compute_bay_bounds(gridlines)

    result = SpotCheckResult(
        saddles_total=len(saddles),
        tolerance_beam=beam_tolerance,
        tolerance_grid=grid_tolerance,
        tolerance_clash=column_tolerance,
    )

    # ── Cross-drawing checks (gated by enabled_checks) ─────────────────────
    cross_types_set = {t for t in CROSS_DRAWING_TYPES if t in enabled_checks}

    for idx, saddle in enumerate(saddles, start=1):
        cx, cy = saddle["centroid"]

        # 1) Nearest STR beam (in STR coords → transform saddle to STR space first)
        sx, sy = _affine_to_mech(ax, ay, bx, by, cx, cy)
        beam_dist, beam_seg = _nearest_beam_distance(sx, sy, beams)

        # 1b) Nearest STR bolt — saddles bolted to structure count as supported
        bolt_dist = min(
            (math.hypot(sx - bx_s, sy - by_s) for bx_s, by_s in bolts),
            default=float("inf"),
        )

        # 2) Nearest gridline (in STR coords)
        grid_dist, grid_kind = _nearest_gridline_distance(sx, sy, gridlines)

        # 3) Column clash (transform footprint to STR coords)
        footprint_str = [_affine_to_mech(ax, ay, bx, by, p[0], p[1])
                         for p in saddle["footprint"]]
        col_seg = _column_collision(footprint_str, columns, column_tolerance)

        # 4) Bay overhang (saddle footprint must stay inside STR bay)
        in_bay = _seg_in_bay(footprint_str, bay) if bay else True

        # Use the closer of (beam, bolt) as the support distance
        support_dist = min(beam_dist, bolt_dist)

        # Bolted connection: if saddle is within beam_tolerance of a bolt,
        # it's intentionally bolted to the structure — skip COLUMN_CLASH
        # (it's a connection, not a clash) and skip NO_BEAM_SUPPORT
        # (the bolt IS the support).
        is_bolted = bolt_dist <= beam_tolerance

        # Issue ordering: most-severe first
        if col_seg is not None and not is_bolted and "COLUMN_CLASH" in cross_types_set:
            result.issues.append(Issue(
                id="", type="COLUMN_CLASH", severity="CRITICAL",
                mech_x=cx, mech_y=cy,
                mech_block=saddle["block_name"], mech_entity=saddle["handle"],
                str_block=col_seg[3], str_entity=col_seg[2],
                distance=support_dist, note="Saddle footprint overlaps STR column",
            ))
            continue
        if support_dist > beam_tolerance and "NO_BEAM_SUPPORT" in cross_types_set:
            result.issues.append(Issue(
                id="", type="NO_BEAM_SUPPORT", severity="CRITICAL",
                mech_x=cx, mech_y=cy,
                mech_block=saddle["block_name"], mech_entity=saddle["handle"],
                str_block=beam_seg[3] if beam_seg else "", str_entity=beam_seg[2] if beam_seg else "",
                distance=support_dist,
                note=f"No beam/bolt within {beam_tolerance:.0f}mm (closest: {support_dist:.0f}mm)",
            ))
            continue
        if not in_bay and "BAY_OVERHANG" in cross_types_set:
            result.issues.append(Issue(
                id="", type="BAY_OVERHANG", severity="WARNING",
                mech_x=cx, mech_y=cy,
                mech_block=saddle["block_name"], mech_entity=saddle["handle"],
                str_block="", str_entity="",
                distance=grid_dist, note="Saddle extends beyond STR bay",
            ))
            continue
        if grid_dist > grid_tolerance and "GRID_MISALIGN" in cross_types_set:
            # INFO-level issue: saddle has structural support but is off-grid.
            # Don't penalize saddles_supported for this — it's a layout
            # concern, not a structural problem.
            result.issues.append(Issue(
                id="", type="GRID_MISALIGN", severity="INFO",
                mech_x=cx, mech_y=cy,
                mech_block=saddle["block_name"], mech_entity=saddle["handle"],
                str_block="", str_entity="",
                distance=grid_dist,
                note=f"Offset {grid_dist:.0f}mm from nearest {grid_kind} gridline",
            ))
            result.saddles_supported += 1   # structural support IS present
            continue

        result.saddles_supported += 1

    # ── Merge MECH intra + STR intra (each runs its own detector module) ───
    # These modules have ALREADY been invoked by pipelines.py before spotcheck.
    # We just read their JSON outputs and concatenate the issues.
    intra_issues: list[Issue] = []

    mech_intra_path = root / "stages" / "mech_intra.json"
    if mech_intra_path.exists():
        try:
            mech_intra_data = json.loads(mech_intra_path.read_text())
            for raw in mech_intra_data.get("issues", []):
                intra_issues.append(Issue(**raw))
        except Exception as e:
            print(f"  spotcheck: failed to merge mech_intra: {e}")

    str_intra_path = root / "stages" / "str_intra.json"
    if str_intra_path.exists():
        try:
            str_intra_data = json.loads(str_intra_path.read_text())
            for raw in str_intra_data.get("issues", []):
                intra_issues.append(Issue(**raw))
        except Exception as e:
            print(f"  spotcheck: failed to merge str_intra: {e}")

    # Filter intra issues by enabled_checks
    intra_filtered = [i for i in intra_issues if i.type in enabled_checks]

    # Renumber cross-drawing issues as ISSUE-NNN (intra issues already carry
    # MECH-/STR- prefixes and don't conflict with these).
    for idx, iss in enumerate(result.issues, start=1):
        iss.id = f"ISSUE-{idx:03d}"

    # Final ordering: severity (CRITICAL > WARNING > INFO), then id
    sev_rank = {"CRITICAL": 0, "WARNING": 1, "INFO": 2, "": 3}

    # Cross-drawing issues ALWAYS included (primary purpose of spotcheck).
    cross_issues = list(result.issues)

    # ── Cluster intra issues: group issues of the same type within ────────
    # cluster_radius of each other into one representative finding.
    # e.g. 200 beam-column clashes inside one bay → "15 clashed beams clustered here"
    intra_capped = _cluster_intra(intra_filtered, radius=cluster_radius)

    # Apply remaining cap after clustering (clustering usually shrinks the list)
    truncated = False
    remaining_capacity = max(0, max_issues - len(cross_issues))
    if len(intra_capped) > remaining_capacity:
        intra_capped = intra_capped[:remaining_capacity]
        truncated = True

    all_issues = cross_issues + intra_capped
    all_issues.sort(key=lambda i: (sev_rank.get(i.severity, 9), i.id))

    result.issues = all_issues

    # Persist merged JSON
    out = root / "stages" / "spotcheck.json"
    out.write_text(json.dumps({
        "saddles_total": result.saddles_total,
        "saddles_supported": result.saddles_supported,
        "tolerance_beam": result.tolerance_beam,
        "tolerance_grid": result.tolerance_grid,
        "tolerance_clash": result.tolerance_clash,
        "max_issues": max_issues,
        "truncated": truncated,
        "enabled_checks": list(enabled_checks),
        "issues": [asdict(i) for i in result.issues],
    }, indent=2))

    return result
