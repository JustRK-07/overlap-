#!/usr/bin/env python3
"""core/inspect.py — Stage 2 (real).

Counts entities by type + lists layers (no rendering in MVP).

Writes jobs/<id>/stages/inspect.json with:
  {
    "drawing_a": {
      "entity_types": {"LINE": 1234, "INSERT": 567, ...},
      "layers": [{"name": "CEN", "color": 1}, ...],
      "extents": {...}
    },
    "drawing_b": { ... }
  }
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import ezdxf

from jobs import job_dir


def _inspect_dxf(path: Path) -> dict:
    """Return entity-type histogram + layer list for a DXF."""
    try:
        doc = ezdxf.readfile(str(path))
    except Exception as e:
        return {"error": str(e)}

    msp = doc.modelspace()

    # Count by dxftype
    type_counter = Counter()
    for e in msp:
        try:
            type_counter[e.dxftype()] += 1
        except Exception:
            pass

    # Layer list (with ACI color)
    layers = []
    for L in doc.layers:
        try:
            name = L.dxf.name
            color = L.dxf.color if L.dxf.hasattr("color") else 7
            layers.append({"name": name, "color": int(color)})
        except Exception:
            continue

    # Extents
    try:
        ext = msp.bbox()
        extents = {
            "min": [float(ext.extmin[0]), float(ext.extmin[1])],
            "max": [float(ext.extmax[0]), float(ext.extmax[1])],
        }
    except Exception:
        extents = None

    # Top-10 entity types (full list is in jobs/<id>/stages/full_types.json
    # only if needed)
    top_types = dict(type_counter.most_common(10))

    return {
        "entity_types": top_types,
        "total_entities": sum(type_counter.values()),
        "layer_count": len(layers),
        "layers": layers[:60],   # cap at 60 for display
        "extents": extents,
    }


def run(job_id: str) -> None:
    d = job_dir(job_id)
    uploads = d / "uploads"
    out = d / "stages"
    out.mkdir(exist_ok=True)

    info = {}
    for path in sorted(uploads.iterdir()):
        if path.suffix.lower() != ".dxf":
            # Skip DWG inspection (we can't read it without conversion)
            info[path.name] = {"skipped": "DWG inspection requires conversion first"}
            continue
        info[path.name] = _inspect_dxf(path)

    (out / "inspect.json").write_text(json.dumps(info, indent=2))
