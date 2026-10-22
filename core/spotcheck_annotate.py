#!/usr/bin/env python3
"""core/spotcheck_annotate.py — Mark spot-check issues on a DXF.

For each CRITICAL or WARNING issue, draw a small numbered marker:
  - A filled circle at the issue location (red=CRIT, orange=WARN)
  - The issue number centered inside the circle

INFO issues are intentionally NOT marked on the DXF — they're reported
in the JSON/DOCX but don't clutter the overlay.

The annotation style is intentionally minimal — a small numbered dot —
so the equipment underneath remains visible and the eye is drawn to
each marked point. The previous bumpy-cloud callout was visually heavy
and obscured geometry.

All annotation geometry is placed on a dedicated layer SPOTCHECK_CLOUDS:
  - CRITICAL → ACI 1  (red)   — filled circle with white number inside
  - WARNING  → ACI 30 (orange) — filled circle with white number inside
  - INFO     → not drawn on DXF

The issue's full ID + details are in the DOCX report — the DXF marker
just shows the short number.

Also writes a DOCX report (severity-colored sections) and mirrors the
JSON to out/.

Reads:  jobs/<id>/out/superimposed.dxf
        jobs/<id>/stages/spotcheck.json
Writes: jobs/<id>/out/superimposed_annotated.dxf
        jobs/<id>/out/spotcheck.docx
        jobs/<id>/out/spotcheck.json
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import ezdxf
from ezdxf.enums import TextEntityAlignment

from jobs import job_dir


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

ANNOTATION_LAYER = "SPOTCHECK_CLOUDS"

# Severity → ACI color (AutoCAD Color Index)
SEVERITY_COLOR = {
    "CRITICAL": 1,    # red
    "WARNING":  30,   # orange/brown
}
# Severities that get a marker on the DXF. INFO excluded by default —
# reported in JSON/DOCX only, not drawn.
ANNOTATABLE_SEVERITIES = ("CRITICAL", "WARNING")

# Marker geometry (in MECH drawing units — millimetres)
MARKER_RADIUS = 350.0      # radius of the marker circle
MARKER_TEXT_COLOR = 7      # ACI 7 = white — visible on red/orange
MARKER_TEXT_HEIGHT = 350.0 # text inside the circle


# ─────────────────────────────────────────────────────────────────────────────
# Marker geometry
# ─────────────────────────────────────────────────────────────────────────────

def _short_id(issue_id: str) -> str:
    """Strip the prefix off an issue id so the number fits in the marker.

    'ISSUE-001' → '1'
    'MECH-007'  → '7'
    'STR-042'   → '42'
    """
    m = re.search(r"(\d+)$", issue_id)
    if m:
        # Strip leading zeros so it fits better visually
        return str(int(m.group(1)))
    return issue_id


def _draw_marker(msp, layer, color, cx, cy, radius=MARKER_RADIUS):
    """Draw a filled circle at (cx, cy). ACI color."""
    return msp.add_circle((cx, cy), radius, dxfattribs={
        "layer": layer,
        "color": color,
    })


def _draw_marker_text(msp, layer, color, text, cx, cy):
    """Draw text centered on (cx, cy) — used for the issue number inside the marker."""
    t = msp.add_text(
        text,
        dxfattribs={
            "layer": layer,
            "color": color,
            "height": MARKER_TEXT_HEIGHT,
            "style": "Standard",
        },
    )
    # Center the text on the marker
    t.set_placement((cx, cy), align=TextEntityAlignment.MIDDLE_CENTER)
    return t


# ─────────────────────────────────────────────────────────────────────────────
# DXF annotation
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_layer(doc, name: str, color: int) -> None:
    """Create the SPOTCHECK_CLOUDS layer if missing."""
    if name in {l.dxf.name for l in doc.layers}:
        return
    doc.layers.add(name=name, color=color)


def annotate_dxf(job_id: str, *, pair_dir=None) -> int:
    """Mark CRITICAL/WARNING issues on the superimposed DXF with small
    numbered colored circles. INFO issues are skipped.

    `pair_dir` switches artifact resolution to the per-pair root.

    Returns the number of markers drawn.
    """
    if pair_dir is not None:
        root = Path(pair_dir).parent
    else:
        root = job_dir(job_id)
    issues_path = root / "stages" / "spotcheck.json"
    if not issues_path.exists():
        raise RuntimeError(f"spotcheck.json missing at {issues_path}")
    issues_data = json.loads(issues_path.read_text())
    issues = issues_data.get("issues", [])

    src = root / "out" / "superimposed.dxf"
    if not src.exists():
        raise RuntimeError(f"superimposed.dxf missing at {src}")
    doc = ezdxf.readfile(str(src))
    _ensure_layer(doc, ANNOTATION_LAYER, SEVERITY_COLOR["CRITICAL"])
    msp = doc.modelspace()

    n_drawn = 0
    for issue in issues:
        sev = issue.get("severity", "")
        if sev not in ANNOTATABLE_SEVERITIES:
            continue  # skip INFO — reported in JSON/DOCX only

        cx = issue["mech_x"]
        cy = issue["mech_y"]
        # 1) Filled colored circle at the issue point
        _draw_marker(msp, ANNOTATION_LAYER, SEVERITY_COLOR[sev], cx, cy)
        # 2) Issue number centered inside the circle (white text)
        _draw_marker_text(msp, ANNOTATION_LAYER, MARKER_TEXT_COLOR,
                          _short_id(issue["id"]), cx, cy)
        n_drawn += 1

    # Save annotated copy alongside the original
    out = root / "out" / "superimposed_annotated.dxf"
    doc.saveas(str(out))
    return n_drawn


# ─────────────────────────────────────────────────────────────────────────────
# DOCX report
# ─────────────────────────────────────────────────────────────────────────────

def _build_docx(job_id: str, issues_data: dict, out_path: Path) -> None:
    """Write a DOCX report — one section per issue, severity-colored headings."""
    from docx import Document
    from docx.shared import RGBColor

    doc = Document()

    # Title
    title = doc.add_heading("Spot-Check Report", level=0)
    p = doc.add_paragraph()
    p.add_run(f"Job: {job_id}\n").bold = True
    saddles_total = issues_data.get("saddles_total", 0)
    saddles_supported = issues_data.get("saddles_supported", 0)
    issues_all = issues_data.get("issues", [])
    n_issues = len(issues_all)
    n_critical = sum(1 for i in issues_all if i["severity"] == "CRITICAL")
    n_warning  = sum(1 for i in issues_all if i["severity"] == "WARNING")
    n_info     = sum(1 for i in issues_all if i["severity"] == "INFO")
    n_drawn    = sum(1 for i in issues_all if i["severity"] in ANNOTATABLE_SEVERITIES)
    p.add_run(
        f"Detected {saddles_total} MECH saddles, "
        f"{saddles_supported} supported, "
        f"{n_issues} issue(s) flagged "
        f"({n_critical} critical, {n_warning} warning, {n_info} info).\n"
    )
    p.add_run(
        f"Annotated DXF shows {n_drawn} cloud callout(s) — "
        f"CRITICAL (red) and WARNING (orange). "
        f"{n_info} INFO-level finding(s) excluded from the overlay."
    ).italic = True

    # Summary table
    doc.add_heading("Tolerances", level=1)
    table = doc.add_table(rows=1, cols=2)
    table.style = "Light List"
    hdr = table.rows[0].cells
    hdr[0].text = "Parameter"
    hdr[1].text = "Value"
    for k, label in [
        ("tolerance_beam", "Beam support radius (mm)"),
        ("tolerance_grid", "Grid alignment tolerance (mm)"),
        ("tolerance_clash", "Column clash tolerance (mm)"),
    ]:
        row = table.add_row().cells
        row[0].text = label
        row[1].text = f"{issues_data.get(k, 0):.0f}"

    # Issue list — grouped by severity
    doc.add_heading("Issues", level=1)
    if not issues_all:
        doc.add_paragraph("No issues detected.")

    sev_order = ["CRITICAL", "WARNING", "INFO"]
    for sev in sev_order:
        sev_issues = [i for i in issues_all if i["severity"] == sev]
        if not sev_issues:
            continue
        # Severity subheading
        h = doc.add_heading(f"{sev} ({len(sev_issues)})", level=2)
        if sev == "CRITICAL":
            color = RGBColor(0xC0, 0x00, 0x00)
        elif sev == "WARNING":
            color = RGBColor(0xC0, 0x70, 0x00)
        else:
            color = RGBColor(0x40, 0x40, 0x40)
        for run in h.runs:
            run.font.color.rgb = color
            run.bold = True

        for issue in sev_issues:
            doc.add_heading(issue["id"], level=3)
            doc.add_paragraph(f"Type: {issue['type']}")
            doc.add_paragraph(f"Location (world coords): "
                              f"({issue['mech_x']:.0f}, {issue['mech_y']:.0f})")
            issue_type = issue["type"]
            if issue_type in ("NO_BEAM_SUPPORT", "COLUMN_CLASH",
                              "BAY_OVERHANG", "GRID_MISALIGN"):
                if issue.get("mech_block"):
                    doc.add_paragraph(f"MECH source block: {issue['mech_block']}")
                if issue.get("str_block"):
                    doc.add_paragraph(f"Nearest STR member: {issue['str_block']}")
            elif issue_type in ("EQUIPMENT_OVERLAP", "BUILDING_CLASH",
                                "CLEARANCE_VIOLATION"):
                if issue.get("mech_block"):
                    doc.add_paragraph(f"MECH entity A: {issue['mech_block']}")
                if issue.get("mech_block_b"):
                    doc.add_paragraph(f"MECH entity B: {issue['mech_block_b']}")
            elif issue_type in ("BEAM_COLUMN_CLASH", "BEAM_BEAM_CROSSING"):
                if issue.get("str_block"):
                    doc.add_paragraph(f"STR member A: {issue['str_block']}")
                if issue.get("str_block_b"):
                    doc.add_paragraph(f"STR member B: {issue['str_block_b']}")
            if issue["distance"] > 0:
                doc.add_paragraph(f"Distance: {issue['distance']:.0f} mm")
            if issue.get("note"):
                doc.add_paragraph(f"Note: {issue['note']}")

    doc.save(str(out_path))


# ─────────────────────────────────────────────────────────────────────────────
# Top-level entrypoint
# ─────────────────────────────────────────────────────────────────────────────

def run(job_id: str, *, pair_dir=None) -> None:
    """Annotate the DXF + write DOCX report. Idempotent.

    `pair_dir` switches artifact resolution to the per-pair root for
    multi-pair jobs. When None, the legacy path is taken.
    """
    d = job_dir(job_id)
    if pair_dir is not None:
        root = Path(pair_dir).parent
    else:
        root = d
    issues_path = root / "stages" / "spotcheck.json"
    issues_data = json.loads(issues_path.read_text())

    # 1) Annotate DXF
    n_drawn = annotate_dxf(job_id, pair_dir=pair_dir)

    # 2) DOCX
    out_docx = root / "out" / "spotcheck.docx"
    _build_docx(job_id, issues_data, out_docx)

    # 3) Mirror JSON to out/ for easy download
    out_json = root / "out" / "spotcheck.json"
    out_json.write_text(json.dumps(issues_data, indent=2))

    n_total = len(issues_data.get("issues", []))
    print(f"  spotcheck: {n_drawn} marker(s) drawn "
          f"(of {n_total} total issue(s); INFO excluded)")
    print(f"  spotcheck docx:      {out_docx}")
    print(f"  spotcheck json:      {out_json}")
