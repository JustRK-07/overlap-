#!/usr/bin/env python3
"""core/findings.py — the canonical findings report.

PLAN_MISMATCH §4 and §5.  This module is the *merger*: it combines the
native mismatch findings (what ``core/mismatch`` detected) with the
remaining spot-check issues (what ``core/spotcheck`` found inside one
drawing), ranks them together, and writes the three artefacts the UI and
the API serve:

``out/findings.json``
    the canonical object — ``{status, generated_at, units, summary,
    coverage, warnings, truncated, findings[]}``
``out/findings.csv``
    one row per finding, with ``core.mismatch.model.CSV_COLUMNS`` as the
    header
``out/findings.docx``
    a summary table (counts per severity / kind, coverage, fit status,
    units) and then one section per finding

**"0 findings" must never mean "the check did not run".**  A disabled or
failed mismatch stage produces ``status="disabled"`` / ``status="error"``,
never an empty list under ``status="ok"``.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["run", "merge", "build_payload"]


#: The spot-check issue types whose ``distance`` is a vertex COUNT, not a
#: length — their magnitude cannot be expressed in millimetres.
_NON_LENGTH_TYPES = frozenset({"BUILDING_CLASH"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_json(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def _issue_to_finding(issue: dict, mm_per_unit: float | None):
    """Promote one spot-check issue to a :class:`~core.mismatch.model.Finding`."""
    from core.mismatch.model import Finding

    itype = str(issue.get("type", "UNKNOWN"))
    distance = issue.get("distance")
    magnitude = None
    if mm_per_unit is not None and distance is not None \
            and itype not in _NON_LENGTH_TYPES:
        try:
            magnitude = float(distance) * float(mm_per_unit)
        except (TypeError, ValueError):
            magnitude = None

    try:
        x = float(issue.get("mech_x", 0.0))
        y = float(issue.get("mech_y", 0.0))
    except (TypeError, ValueError):
        x = y = 0.0

    return Finding(
        kind=f"SPOT_{itype}",
        severity=str(issue.get("severity", "WARNING")),
        origin="spotcheck",
        id=str(issue.get("id", "")),
        location=(x, y),
        magnitude_mm=magnitude,
        tolerance_mm=0.0,
        message=str(issue.get("note", "") or ""),
        details={"spotcheck": dict(issue)},
    )


def merge(native_payload: dict | None, spotcheck_payload: dict | None,
          *, mm_per_unit: float | None = None) -> list:
    """The native findings plus the remaining spot-check issues, as Findings."""
    from core.mismatch.model import Finding, finding_to_dict

    merged: list = []

    # ── the native findings ────────────────────────────────────────────
    for raw in (native_payload or {}).get("findings", []) or []:
        # they were serialised by `finding_to_dict`; rebuild the dataclass
        try:
            from core.mismatch.model import Finding as _F
            merged.append(_F(
                kind=raw.get("kind", ""),
                severity=raw.get("severity", "WARNING"),
                origin=raw.get("origin", "mismatch"),
                id=raw.get("id", ""),
                uid=raw.get("uid", ""),
                location=tuple(raw.get("location", (0.0, 0.0)) or (0.0, 0.0)),
                offset_mm=(tuple(raw["offset_mm"])
                           if raw.get("offset_mm") else None),
                magnitude_mm=raw.get("magnitude_mm"),
                tolerance_mm=float(raw.get("tolerance_mm", 0.0) or 0.0),
                ratio=raw.get("ratio"),
                rotation_deg=raw.get("rotation_deg"),
                systematic=bool(raw.get("systematic", False)),
                confidence=raw.get("confidence", "normal"),
                message=raw.get("message", ""),
                details=dict(raw.get("details") or {}),
            ))
        except Exception:
            continue

    # ── the spot-check issues ──────────────────────────────────────────
    for issue in (spotcheck_payload or {}).get("issues", []) or []:
        merged.append(_issue_to_finding(issue, mm_per_unit))

    return merged


def build_payload(root: Path, *, job_id: str = "") -> dict:
    """Read the stage outputs under ``root`` and build the canonical object."""
    from core.mismatch.model import summarize

    native = _load_json(root / "stages" / "mismatch.json")
    spotcheck = _load_json(root / "stages" / "spotcheck.json")

    units = dict((native or {}).get("units") or {})
    mm_per_unit = units.get("mm_per_unit")
    if mm_per_unit is None:
        mm_per_unit = None

    status = str((native or {}).get("status", "error"))
    warnings = list((native or {}).get("warnings") or [])
    coverage = dict((native or {}).get("coverage") or {})

    if status == "disabled":
        return {
            "status": "disabled",
            "generated_at": _now_iso(),
            "units": units,
            "summary": {},
            "coverage": coverage,
            "warnings": warnings or ["mismatch detection is disabled by policy"],
            "truncated": False,
            "findings": [],
        }

    if status == "error":
        message = str((native or {}).get("message")
                      or "the mismatch check did not run")
        return {
            "status": "error",
            "generated_at": _now_iso(),
            "units": units,
            "summary": {},
            "coverage": coverage,
            "warnings": warnings + [message],
            "truncated": False,
            "findings": [],
        }

    merged = merge(native, spotcheck, mm_per_unit=mm_per_unit)

    from core.mismatch.model import rank_findings, truncate
    ranked = rank_findings(merged)
    limit = int((native or {}).get("policy", {}).get("max_findings", 5000))
    ranked, truncated = truncate(ranked, limit)

    from core.mismatch.model import finding_to_dict
    return {
        "status": "ok",
        "generated_at": _now_iso(),
        "units": units,
        "summary": summarize(ranked),
        "coverage": coverage,
        "warnings": warnings,
        "truncated": bool(truncated),
        "findings": [finding_to_dict(f) for f in ranked],
    }


def _build_docx(payload: dict, out_path: Path) -> None:
    """The findings DOCX — a summary table, then one section per finding."""
    from docx import Document

    from core.spotcheck_annotate import _severity_heading

    doc = Document()
    doc.add_heading("Findings Report", level=0)
    p = doc.add_paragraph()
    p.add_run(f"Generated: {payload.get('generated_at', '')}\n").bold = True
    p.add_run(f"Status: {payload.get('status', 'unknown')}")

    summary = payload.get("summary") or {}
    by_sev = summary.get("by_severity") or {}
    by_kind = summary.get("by_kind") or {}
    by_origin = summary.get("by_origin") or {}

    doc.add_heading("Summary", level=1)
    table = doc.add_table(rows=1, cols=2)
    table.style = "Light List"
    hdr = table.rows[0].cells
    hdr[0].text = "Parameter"
    hdr[1].text = "Value"
    rows = [
        ("Total findings", str(summary.get("total", len(payload.get("findings", []))))),
        ("Critical", str(by_sev.get("CRITICAL", 0))),
        ("Warning", str(by_sev.get("WARNING", 0))),
        ("Info", str(by_sev.get("INFO", 0))),
        ("From the mismatch check", str(by_origin.get("mismatch", 0))),
        ("From the spot check", str(by_origin.get("spotcheck", 0))),
        ("Distinct kinds", str(len(by_kind))),
        ("Truncated", str(payload.get("truncated", False))),
    ]
    for label, value in rows:
        row = table.add_row().cells
        row[0].text = label
        row[1].text = value

    units = payload.get("units") or {}
    if units:
        doc.add_heading("Units", level=2)
        doc.add_paragraph(
            f"MECH $INSUNITS = {units.get('mech_insunits')}, "
            f"{units.get('mm_per_unit')} mm per drawing unit"
            + (" (assumed)" if units.get("assumed") else ""))

    coverage = payload.get("coverage") or {}
    if coverage:
        doc.add_heading("Coverage", level=2)
        cov = doc.add_table(rows=1, cols=4)
        cov.style = "Light List"
        hdr = cov.rows[0].cells
        for i, name in enumerate(("check", "items", "matched", "unmatched")):
            hdr[i].text = name
        for name, counts in sorted(coverage.items()):
            row = cov.add_row().cells
            row[0].text = str(name)
            row[1].text = str((counts or {}).get("items", 0))
            row[2].text = str((counts or {}).get("matched", 0))
            row[3].text = str((counts or {}).get("unmatched", 0))

    findings = payload.get("findings") or []
    doc.add_heading("Findings", level=1)
    if not findings:
        doc.add_paragraph("No findings.")
    else:
        for sev in ("CRITICAL", "WARNING", "INFO"):
            group = [f for f in findings if f.get("severity") == sev]
            if not group:
                continue
            _severity_heading(doc, sev, len(group))
            for f in group:
                doc.add_heading(f"{f.get('id', '?')} {f.get('kind', '')}", level=3)
                loc = f.get("location") or (0.0, 0.0)
                doc.add_paragraph(f"Location (MECH world): ({loc[0]:.1f}, {loc[1]:.1f})")
                if f.get("magnitude_mm") is not None:
                    doc.add_paragraph(f"Magnitude: {f['magnitude_mm']:.1f} mm")
                off = f.get("offset_mm")
                if off:
                    doc.add_paragraph(
                        f"Offset (STR - MECH): ({off[0]:.1f}, {off[1]:.1f}) mm")
                if f.get("rotation_deg") is not None:
                    doc.add_paragraph(f"Rotation: {f['rotation_deg']:+.2f} deg")
                doc.add_paragraph(f"Tolerance: {f.get('tolerance_mm', 0.0):.1f} mm")
                if f.get("ratio") is not None:
                    doc.add_paragraph(f"Ratio: {f['ratio']:.2f}")
                for label, ref in (("MECH", f.get("mech_ref")),
                                   ("STR", f.get("str_ref"))):
                    if ref:
                        doc.add_paragraph(
                            f"{label} ref: block {ref.get('block') or '-'}, "
                            f"layer {ref.get('layer') or '-'}, "
                            f"handle {ref.get('handle') or '-'}")
                if f.get("message"):
                    doc.add_paragraph(f"Message: {f['message']}")

    doc.save(str(out_path))


def run(job_id: str, *, pair_dir: Path | None = None) -> None:
    """Write ``out/findings.{json,csv,docx}`` for one pair.

    ``pair_dir`` scopes the run to the pair's root; without it the legacy
    single-pair layout at the job root is used.
    """
    from jobs import job_dir

    from core.mismatch import model as M

    root = Path(pair_dir).parent if pair_dir is not None else job_dir(job_id)
    out_dir = root / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = build_payload(root, job_id=job_id)

    (out_dir / "findings.json").write_text(
        json.dumps(payload, indent=2, default=str))

    # the CSV carries the same rows
    from core.mismatch.model import Finding
    rows = []
    for raw in payload.get("findings", []):
        try:
            rows.append(Finding(
                kind=raw.get("kind", ""),
                severity=raw.get("severity", "WARNING"),
                origin=raw.get("origin", "mismatch"),
                id=raw.get("id", ""),
                uid=raw.get("uid", ""),
                location=tuple(raw.get("location", (0.0, 0.0)) or (0.0, 0.0)),
                offset_mm=(tuple(raw["offset_mm"])
                           if raw.get("offset_mm") else None),
                magnitude_mm=raw.get("magnitude_mm"),
                tolerance_mm=float(raw.get("tolerance_mm", 0.0) or 0.0),
                ratio=raw.get("ratio"),
                rotation_deg=raw.get("rotation_deg"),
                systematic=bool(raw.get("systematic", False)),
                confidence=raw.get("confidence", "normal"),
                message=raw.get("message", ""),
                details=dict(raw.get("details") or {}),
            ))
        except Exception:
            continue
    M.write_csv(rows, out_dir / "findings.csv")

    try:
        _build_docx(payload, out_dir / "findings.docx")
    except Exception as exc:            # noqa: BLE001 — the DOCX is optional
        print(f"  findings DOCX not written: {exc}")
