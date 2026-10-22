#!/usr/bin/env python3
"""core/convert.py — Stage 3.

Convert DWG → DXF when the user's input is DWG. Skipped silently for DXF.

If ODA File Converter is not installed and any input is DWG, this stage
marks the job as failed with a clear message asking the user to export
DXF and re-upload.

Reads:  jobs/<id>/uploads/*.dwg  (and *.dxf files are copied through)
Writes: jobs/<id>/cleaned/<base>.dxf  for all base names
"""

from __future__ import annotations

import shutil
from pathlib import Path

from jobs import job_dir, update_status


def run(job_id: str) -> None:
    d = job_dir(job_id)
    uploads = d / "uploads"
    cleaned = d / "cleaned"
    cleaned.mkdir(exist_ok=True)

    # Check what we have
    dwg_files = sorted(uploads.glob("*.dwg"))
    dxf_files = sorted(uploads.glob("*.dxf"))

    if not dwg_files:
        # Pure DXF input — just copy through to cleaned/ (no transformation)
        for dxf in dxf_files:
            shutil.copy2(dxf, cleaned / dxf.name)
        return

    # We have DWG input — need ODA
    try:
        from ezdxf.addons import odafc
        from ezdxf.addons.odafc import convert as odafc_convert
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

    # Convert each DWG → DXF
    for dwg in dwg_files:
        out_dxf = cleaned / (dwg.stem + ".dxf")
        odafc_convert(str(dwg), str(out_dxf))

    # Copy any DXF inputs through too
    for dxf in dxf_files:
        shutil.copy2(dxf, cleaned / dxf.name)
