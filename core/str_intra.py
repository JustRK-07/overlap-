#!/usr/bin/env python3
"""core/str_intra.py — Clashes within the STR drawing only.

Two independent detectors, both driven from the same beam/column
segment list extracted by walking Part-*/Bolt-*/Connection-*/Line-*
INSERTs and transforming block-local LINEs to world coords.

  1. BEAM_COLUMN_CLASH — a beam end or body comes within tolerance of
                          a column. A beam should NEVER pierce or end
                          inside a column — they're sized independently
                          and the connection is at the column face.
  2. BEAM_BEAM_CROSSING — two beams cross in plan (diagonal members
                          like braces intentionally cross — caller can
                          disable this check).

Output:
  jobs/<id>/stages/str_intra.json
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import ezdxf

from jobs import job_dir
from .geom import point_segment_distance as _point_segment_distance
from .geom import segments_intersect as _seg_intersect
from .smart3d import detect_anchor_pattern as _detect_anchor_pattern
from .smart3d import segment_world_coords as _segment_world_coords
from .smart3d import walk_block_lines as _walk_block_lines


# ─────────────────────────────────────────────────────────────────────────────
# Issue schema
# ─────────────────────────────────────────────────────────────────────────────

ISSUE_TYPES = (
    "BEAM_COLUMN_CLASH",   # CRITICAL — beam inside or piercing a column
    "BEAM_BEAM_CROSSING",  # WARNING — two beams cross in plan
)

SEVERITY = {
    "BEAM_COLUMN_CLASH": "CRITICAL",
    "BEAM_BEAM_CROSSING": "WARNING",
}

# Beam end must not come within this distance of a column.
# Default 200mm — covers typical end-plate + bolt allowance. Beam ends
# within CONNECTION_TOLERANCE_END (set in _detect_beam_column) of a
# column endpoint are skipped because those are intentional connections.
DEFAULT_BEAM_COLUMN_TOLERANCE = 200.0


@dataclass
class Issue:
    """One STR-intra finding."""
    id: str
    type: str
    severity: str
    # Location of the clash point (world coords, STR space)
    mech_x: float        # reused as "clash_x"
    mech_y: float        # reused as "clash_y"
    # The two STR members involved
    str_block: str = ""
    str_entity: str = ""
    str_block_b: str = ""
    str_entity_b: str = ""
    distance: float = 0.0
    note: str = ""


@dataclass
class StrIntraResult:
    beams_total: int = 0
    columns_total: int = 0
    enabled_checks: list[str] = field(default_factory=list)
    tolerance_beam_column: float = DEFAULT_BEAM_COLUMN_TOLERANCE
    issues: list[Issue] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Beam/column extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_members(doc) -> tuple[list, list]:
    """Returns (beams, columns) as lists of ((a,b), handle, block_name).

    Detects Smart3D-style anchor pattern (all INSERTs share the same
    insert+scale) and skips the transform in that case.

    Filters out short column segments (<500mm) which are typically
    column flange cross-sections or end-plate lines, not real columns.
    Real columns in steel structures are typically 3-6m tall.
    """
    is_anchor, _, _, _, _ = _detect_anchor_pattern(doc)
    apply_t = not is_anchor

    # Columns <500mm are typically cross-section marks, not real columns.
    # A real structural column in a steel frame is at least 1-2m long
    # (single-story) or 3-6m (multi-story). Anything shorter is likely a
    # column-stub from a flange mark or end-plate detail.
    MIN_COLUMN_LENGTH = 500.0

    beams, columns = [], []
    for ins in doc.modelspace():
        if ins.dxftype() != "INSERT":
            continue
        nm = ins.dxf.name
        # Skip gridlines — they're not structural members
        if nm.startswith("GridLine-") or nm.startswith(" Line-"):
            continue
        block = None
        for b in doc.blocks:
            if b.name == nm:
                block = b
                break
        if block is None:
            continue
        if apply_t:
            ins_pos, ins_scale = _segment_world_coords(ins)
        else:
            ins_pos, ins_scale = (0.0, 0.0), (1.0, 1.0)
        try:
            handle = ins.dxf.handle if ins.dxf.hasattr("handle") else ""
        except Exception:
            handle = ""
        for (a, b) in _walk_block_lines(block, ins_pos, ins_scale,
                                         apply_transform=apply_t):
            dx = b[0] - a[0]
            dy = b[1] - a[1]
            length = math.hypot(dx, dy)
            if length < 100:
                continue
            seg = ((a, b), handle, nm)
            if abs(dy) < 0.1 * length:
                beams.append(seg)
            elif abs(dx) < 0.1 * length:
                # Only count as a column if long enough — short verticals
                # are flange cross-sections, not real columns.
                if length >= MIN_COLUMN_LENGTH:
                    columns.append(seg)
            else:
                beams.append(seg)
    return beams, columns


# ─────────────────────────────────────────────────────────────────────────────
# Segment-segment intersection helpers
# ─────────────────────────────────────────────────────────────────────────────

def _seg_intersect(p1, p2, p3, p4) -> bool:
    """True if segments p1-p2 and p3-p4 properly cross (interior crossing)."""
    def ccw(A, B, C):
        return (C[1] - A[1]) * (B[0] - A[0]) > (B[1] - A[1]) * (C[0] - A[0])
    return ccw(p1, p3, p4) != ccw(p2, p3, p4) and \
           ccw(p1, p2, p3) != ccw(p1, p2, p4)


# ─────────────────────────────────────────────────────────────────────────────
# Detectors
# ─────────────────────────────────────────────────────────────────────────────


def _detect_beam_column(beams: list, columns: list, tolerance: float) -> list[Issue]:
    """A beam ENDPOINT pierces a column BODY → CRITICAL.

    For each beam endpoint, finds the BEST-MATCHING column (closest
    perpendicular distance). If that closest column is a connection
    (beam framing into column face, or beam end at column endpoint),
    the beam is supported — skip. Otherwise it's a clash.

    Filters out legitimate beam-column connections:
      - Beam endpoint close to a column ENDPOINT (start/end) → connection
        (typical end-plate + bolt tolerance is ~150mm).
      - Beam endpoint close to the column FACE (perpendicular distance)
        AND projects inside the column body → normal side connection
        (beam framing into column face).
      - Beam endpoint projects OUTSIDE the column body (t outside [0,1])
        → beam end is at or past the column face, normal connection.

    Skips pairs where the beam and column come from the SAME block.
    """
    # Beam endpoint within this distance of a column endpoint (start or end)
    # is treated as a normal beam-column connection (end-plate + bolt gap),
    # not a clash. 250mm covers typical end-plate + bolt allowance plus
    # drawing tolerance / CAD model approximation error.
    CONNECTION_TOLERANCE_END = 250.0
    # Same for perpendicular distance to column face: a beam framing into
    # the column's SIDE within this distance is a connection, not a clash.
    CONNECTION_TOLERANCE_FACE = 100.0
    issues = []
    for beam_seg in beams:
        (a, b), b_handle, b_name = beam_seg
        for bp in (a, b):
            best_d_perp = float("inf")
            best_t = 0.0
            best_col_name = ""
            best_col_handle = ""
            best_was_connection = False

            for col_seg in columns:
                (c, d), c_handle, c_name = col_seg
                if b_handle == c_handle or b_name == c_name:
                    continue

                # Project beam endpoint onto column line
                dx = d[0] - c[0]
                dy = d[1] - c[1]
                seg_len_sq = dx * dx + dy * dy
                if seg_len_sq == 0:
                    continue
                t = ((bp[0] - c[0]) * dx + (bp[1] - c[1]) * dy) / seg_len_sq
                proj_x = c[0] + t * dx
                proj_y = c[1] + t * dy
                d_perp = math.hypot(bp[0] - proj_x, bp[1] - proj_y)

                # Only consider columns whose projection lies on the body
                # (t ∈ [0, 1]) — those are within the column's length.
                # Columns with t outside [0, 1] are not "pierced" by the
                # beam endpoint.
                if t < 0.0 or t > 1.0:
                    continue

                # Skip if this is not the closest column yet — we'll only
                # evaluate the BEST match for clash vs connection.
                if d_perp >= best_d_perp:
                    continue
                best_d_perp = d_perp
                best_t = t
                best_col_name = c_name
                best_col_handle = c_handle

            if best_d_perp == float("inf"):
                continue  # No column had the beam endpoint within its body

            # Now classify the BEST match:
            # - Close to column endpoint → connection
            # - Close to column face → connection
            # - Otherwise → clash (if within tolerance)
            # Find the column segment corresponding to the best match (re-scan
            # to get its endpoints for the endpoint-distance check).
            best_c = best_d = None
            for col_seg in columns:
                (c2, d2), c2_handle, c2_name = col_seg
                if b_handle == c2_handle or b_name == c2_name:
                    continue
                dx2 = d2[0] - c2[0]
                dy2 = d2[1] - c2[1]
                seg_len_sq2 = dx2 * dx2 + dy2 * dy2
                if seg_len_sq2 == 0:
                    continue
                t2 = ((bp[0] - c2[0]) * dx2 + (bp[1] - c2[1]) * dy2) / seg_len_sq2
                if t2 < 0.0 or t2 > 1.0:
                    continue
                proj_x2 = c2[0] + t2 * dx2
                proj_y2 = c2[1] + t2 * dy2
                d_perp2 = math.hypot(bp[0] - proj_x2, bp[1] - proj_y2)
                if abs(d_perp2 - best_d_perp) < 0.01:
                    best_c, best_d = c2, d2
                    break

            if best_c is None:
                # Fallback — best match not found (shouldn't happen)
                if best_d_perp <= tolerance:
                    issues.append(Issue(
                        id="", type="BEAM_COLUMN_CLASH", severity="CRITICAL",
                        mech_x=bp[0], mech_y=bp[1],
                        str_block=b_name, str_entity=b_handle,
                        str_block_b=best_col_name, str_entity_b=best_col_handle,
                        distance=best_d_perp,
                        note=f"Beam end {best_d_perp:.0f}mm inside column body (t={best_t:.2f})",
                    ))
                continue

            d_to_c_start = math.hypot(bp[0] - best_c[0], bp[1] - best_c[1])
            d_to_c_end   = math.hypot(bp[0] - best_d[0], bp[1] - best_d[1])
            is_endpoint_connection = min(d_to_c_start, d_to_c_end) <= CONNECTION_TOLERANCE_END
            is_face_connection = best_d_perp <= CONNECTION_TOLERANCE_FACE

            if is_endpoint_connection or is_face_connection:
                continue  # normal connection, not a clash

            if best_d_perp <= tolerance:
                issues.append(Issue(
                    id="",
                    type="BEAM_COLUMN_CLASH",
                    severity="CRITICAL",
                    mech_x=bp[0], mech_y=bp[1],
                    str_block=b_name,
                    str_entity=b_handle,
                    str_block_b=best_col_name,
                    str_entity_b=best_col_handle,
                    distance=best_d_perp,
                    note=f"Beam end {best_d_perp:.0f}mm inside column body (t={best_t:.2f})",
                ))
    return issues


def _detect_beam_beam(beams: list) -> list[Issue]:
    """Two beams crossing in plan → WARNING.

    Skips pairs from the SAME block (same structural assembly).
    """
    issues = []
    n = len(beams)
    for i in range(n):
        (a, b), bh, bn = beams[i]
        for j in range(i + 1, n):
            (c, d), ch, cn = beams[j]
            if bh == ch or bn == cn:
                continue
            if a == c and b == d:
                continue
            if a == d and b == c:
                continue
            if _seg_intersect(a, b, c, d):
                mx = (a[0] + b[0] + c[0] + d[0]) / 4
                my = (a[1] + b[1] + c[1] + d[1]) / 4
                issues.append(Issue(
                    id="",
                    type="BEAM_BEAM_CROSSING",
                    severity="WARNING",
                    mech_x=mx, mech_y=my,
                    str_block=bn,
                    str_entity=bh,
                    str_block_b=cn,
                    str_entity_b=ch,
                    distance=0.0,
                    note=f"Beam crosses beam ({bn} ↔ {cn})",
                ))
    return issues


# ─────────────────────────────────────────────────────────────────────────────
# Public entrypoint
# ─────────────────────────────────────────────────────────────────────────────

def run(job_id: str, *,
        enabled_checks: list[str] | None = None,
        beam_column_tolerance: float = DEFAULT_BEAM_COLUMN_TOLERANCE,
        max_issues: int = 500,
        pair_dir: Path | None = None) -> StrIntraResult:
    """Stage 7.5b: detect clashes within the STR drawing only.

    enabled_checks: subset of {BEAM_COLUMN_CLASH, BEAM_BEAM_CROSSING}.
    None means run all.

    `pair_dir` is the per-pair cleaned dir for multi-pair runs.
    When None, the legacy single-job path is taken — bit-equivalent.

    max_issues caps the total count to keep DXF rendering fast and the
    DOCX report readable. Default 500 — beyond that, we truncate with
    a note in the output JSON.
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
    # STR is alphabetically second
    str_path = cleaned[1] if len(cleaned) > 1 else None
    if str_path is None or not str_path.exists():
        raise RuntimeError("STR cleaned DXF missing")

    doc = ezdxf.readfile(str(str_path))
    beams, columns = _extract_members(doc)

    result = StrIntraResult(
        beams_total=len(beams),
        columns_total=len(columns),
        enabled_checks=list(enabled_checks),
        tolerance_beam_column=beam_column_tolerance,
    )

    all_issues: list[Issue] = []

    # Run detectors in priority order — critical issues first
    if "BEAM_COLUMN_CLASH" in enabled_checks:
        all_issues.extend(_detect_beam_column(beams, columns, beam_column_tolerance))
    if "BEAM_BEAM_CROSSING" in enabled_checks:
        all_issues.extend(_detect_beam_beam(beams))

    truncated = False
    if len(all_issues) > max_issues:
        # Keep critical first
        sev_rank = {"CRITICAL": 0, "WARNING": 1, "INFO": 2, "": 3}
        all_issues.sort(key=lambda i: (sev_rank.get(i.severity, 9), i.distance))
        all_issues = all_issues[:max_issues]
        truncated = True

    for idx, iss in enumerate(all_issues, start=1):
        iss.id = f"STR-{idx:03d}"
    result.issues = all_issues

    out = root / "stages" / "str_intra.json"
    out.write_text(json.dumps({
        "beams_total": result.beams_total,
        "columns_total": result.columns_total,
        "enabled_checks": result.enabled_checks,
        "tolerance_beam_column": result.tolerance_beam_column,
        "max_issues": max_issues,
        "truncated": truncated,
        "issues": [asdict(i) for i in result.issues],
    }, indent=2))

    return result
