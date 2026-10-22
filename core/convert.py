#!/usr/bin/env python3
"""core/convert.py — Stage 3.

Convert DWG → DXF when the user's input is DWG. Skipped silently for DXF.

If ODA File Converter is not installed and any input is DWG, this stage
marks the job as failed with a clear message asking the user to export
DXF and re-upload.

Reads:  jobs/<id>/uploads/*.dwg  (and *.dxf are normalized through core.dxfio)
Writes: jobs/<id>/converted/<base>.dxf  for all base names (audited, normalized)
"""

from __future__ import annotations

from pathlib import Path

from jobs import job_dir, update_status


def run(job_id: str) -> None:
    from jobs import load_job
    from .roles import converted_rel, entries_from_job, write_manifest

    d = job_dir(job_id)
    uploads = d / "uploads"

    job = load_job(job_id)
    if job is None:
        raise RuntimeError(f"no such job: {job_id}")

    # Roles come from job.json (the API), never from names. Write the
    # manifest first so every later stage reads the same roles.
    entries = entries_from_job(job)
    stems = [Path(e.name).stem for e in entries]
    if len(set(stems)) != len(stems):
        raise RuntimeError(
            "two uploads share the same base name "
            f"({sorted(s for s in set(stems) if stems.count(s) > 1)}); rename one."
        )
    write_manifest(job_id, entries)

    # Only convert/normalize the uploads the manifest knows about.
    dwg_files = [uploads / e.name for e in entries
                 if e.name.lower().endswith(".dwg") and (uploads / e.name).exists()]
    dxf_files = [uploads / e.name for e in entries
                 if e.name.lower().endswith(".dxf") and (uploads / e.name).exists()]

    (d / "converted").mkdir(exist_ok=True)

    if dwg_files:
        try:
            from ezdxf.addons import odafc
        except ImportError:
            raise RuntimeError(
                "DWG input detected but ezdxf.addons.odafc not available. "
                "Please save your DWG as DXF in AutoCAD and re-upload."
            )
        if not odafc.is_installed():
            raise RuntimeError(
                "ODA File Converter is not installed. "
                "Please save your DWG as DXF in AutoCAD and re-upload, "
                "or install ODA File Converter and retry."
            )
        # DWG -> DXF. replace=True makes re-runs idempotent (odafc refuses
        # to overwrite an existing file otherwise).
        for dwg in dwg_files:
            out_dxf = d / converted_rel(dwg.name)
            odafc.convert(str(dwg), str(out_dxf), version="R2018",
                          audit=True, replace=True)
            # Normalize the ODA output the same way as a DXF upload.
            _normalize(job_id, d, out_dxf, out_dxf)

    # DXF: recover-read + audit + save into converted/ (no blind copy).
    for dxf in dxf_files:
        _normalize(job_id, d, dxf, d / converted_rel(dxf.name))


def _normalize(job_id: str, d: Path, src: Path, dst: Path) -> None:
    from . import dxfio
    doc, summary = dxfio.read_input(src)
    save_summary = dxfio.save(doc, dst)
    summary = {**summary, "fixes": summary["fixes"] + save_summary["fixes"],
               "errors": (summary["errors"] + save_summary["errors"])[:50]}
    dxfio.record_audit(d, "convert", dst.name, summary)
