"""core/dxfio.py — the single place DXF files are read and written.

* ``read_input``    uploads / converted files: forgiving ``recover.readfile``
                    followed by ``doc.audit()``; the audit result is returned
                    so stages can record it in ``stages/audit.json``.
* ``read_internal`` pipeline-written files (cleaned, superimposed):
                    strict ``ezdxf.readfile`` + ``doc.audit()``.
* ``save``          ``doc.audit()`` then ``saveas``.

``core/package.py`` deliberately keeps its own readfile-first logic until
step 8 (see PLAN.md, decision 8).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import recover

MAX_ERRORS = 50


class DXFReadError(RuntimeError):
    """A DXF could not be read or is still broken after audit."""


AuditSummary = dict  # {"fixes": int, "errors": [str, ...]}


def mm_per_unit(doc) -> float:
    """How many millimetres one drawing unit of ``doc`` is worth.

    Read from ``$INSUNITS``.  A drawing that declares no units (or an
    unknown code) is **assumed** to be in millimetres — which is what the
    rest of the pipeline assumes too.

    Every geometric threshold in the project is specified in millimetres
    (the plan says so), so the modules that work in *drawing* units must
    divide their thresholds by this factor.  Getting it wrong is not a
    rounding error: on a metre-scale STR drawing a 100 mm threshold
    becomes 100 m, and every member of the drawing is discarded.
    """
    try:
        code = int(doc.header.get("$INSUNITS", 0))
    except Exception:
        code = 0
    if code <= 0:
        return 1.0
    try:
        factor = float(ezdxf.units.conversion_factor(code, 4))  # 4 == mm
    except Exception:
        return 1.0
    if not (factor > 0.0) or factor != factor:      # not finite / NaN
        return 1.0
    return factor


def _summarize(auditor, extra_fixes: int = 0) -> AuditSummary:
    return {
        "fixes": len(auditor.fixes) + extra_fixes,
        "errors": [str(e.message) for e in auditor.errors][:MAX_ERRORS],
    }


def read_input(path) -> tuple[Any, AuditSummary]:
    """Recover-read an input file, audit it, return ``(doc, summary)``."""
    try:
        doc, rec_auditor = recover.readfile(str(path))
    except Exception as e:  # IOError, DXFStructureError, ...
        raise DXFReadError(f"cannot read {Path(path).name}: {e}") from e
    auditor = doc.audit()
    summary = _summarize(auditor, extra_fixes=len(rec_auditor.fixes))
    if auditor.has_errors:
        msgs = summary["errors"][:5]
        raise DXFReadError(
            f"{Path(path).name} still has errors after recovery: {msgs}"
        )
    return doc, summary


def read_internal(path) -> Any:
    """Strict read of a pipeline-written DXF (+ audit). Raises on errors."""
    doc = ezdxf.readfile(str(path))
    auditor = doc.audit()
    if auditor.has_errors:
        msgs = [str(e.message) for e in auditor.errors][:5]
        raise DXFReadError(f"{Path(path).name} failed audit: {msgs}")
    return doc


def save(doc, path) -> AuditSummary:
    """Audit then save. Returns the audit summary."""
    auditor = doc.audit()
    doc.saveas(str(path))
    return _summarize(auditor)


def record_audit(job_dir_path, stage: str, filename: str, summary: AuditSummary) -> None:
    """Merge ``{stage}:{filename}`` into ``<job>/stages/audit.json``."""
    stages = Path(job_dir_path) / "stages"
    stages.mkdir(parents=True, exist_ok=True)
    p = stages / "audit.json"
    try:
        data = json.loads(p.read_text())
    except Exception:
        data = {}
    data[f"{stage}:{filename}"] = summary
    p.write_text(json.dumps(data, indent=2))
