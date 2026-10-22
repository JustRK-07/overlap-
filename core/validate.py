#!/usr/bin/env python3
"""core/validate.py — Stage 1 (real).

Reads each uploaded file with ezdxf.recover.readfile(). For DWG files
falls back to listing existence only (full DWG read needs ODA Converter).

Writes jobs/<id>/stages/validate.json with:
  {
    "drawing_a": {"name", "size", "format", "readable", "entity_count", "layer_count", "extents"},
    "drawing_b": { ... same ... }
  }
"""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
from ezdxf import recover

from jobs import job_dir


def _validate_dxf(path: Path) -> dict:
    """Use ezdxf.recover to read the DXF, gather basic metadata."""
    try:
        # Use ezdxf.recover for forgiving parse — tolerates corrupt entities
        doc, auditor = recover.readfile(str(path))
        if auditor.has_errors:
            # Recovered with errors — still usable; just note it
            recoverable = True
        else:
            recoverable = False

        msp = doc.modelspace()
        ent_count = sum(1 for _ in msp)
        layer_count = len(doc.layers)
        try:
            ext = msp.bbox()
            extents = {
                "min": [ext.extmin[0], ext.extmin[1]],
                "max": [ext.extmax[0], ext.extmax[1]],
            }
        except Exception:
            extents = None

        # Detect DXF version string (R2018, R2013, etc.)
        try:
            version = doc.dxfversion
        except AttributeError:
            version = "Unknown"

        return {
            "format": "DXF",
            "version": version,
            "readable": True,
            "recovered_with_errors": recoverable,
            "entity_count": ent_count,
            "layer_count": layer_count,
            "extents": extents,
        }
    except Exception as e:
        return {
            "format": "DXF",
            "readable": False,
            "error": str(e),
            "entity_count": 0,
            "layer_count": 0,
            "extents": None,
        }


def _validate_dwg(path: Path) -> dict:
    """DWG can't be parsed without ODA — note that here."""
    try:
        from ezdxf.addons import odafc
        if odafc.is_installed():
            return {
                "format": "DWG",
                "readable": True,
                "note": "DWG conversion available via ODA",
                "entity_count": -1,    # filled in after conversion
                "layer_count": -1,
                "extents": None,
            }
        else:
            return {
                "format": "DWG",
                "readable": False,
                "error": "ODA File Converter not installed; please save as DXF and re-upload",
                "entity_count": 0,
                "layer_count": 0,
                "extents": None,
            }
    except ImportError:
        return {
            "format": "DWG",
            "readable": False,
            "error": "DWG support requires ezdxf.addons.odafc",
            "entity_count": 0,
            "layer_count": 0,
            "extents": None,
        }


def run(job_id: str) -> None:
    """Validate each uploaded file."""
    d = job_dir(job_id)
    uploads_dir = d / "uploads"
    out_dir = d / "stages"
    out_dir.mkdir(exist_ok=True)

    info = {}
    for path in sorted(uploads_dir.iterdir()):
        ext = path.suffix.lower()
        if ext == ".dxf":
            data = _validate_dxf(path)
        elif ext == ".dwg":
            data = _validate_dwg(path)
        else:
            data = {"format": ext.upper(), "readable": False,
                    "error": f"Unsupported extension: {ext}"}
        info[path.name] = {
            "size_bytes": path.stat().st_size,
            **data,
        }

    (out_dir / "validate.json").write_text(json.dumps(info, indent=2))
