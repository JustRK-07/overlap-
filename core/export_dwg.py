#!/usr/bin/env python3
"""core/export_dwg.py — the optional DWG export (PLAN_MISMATCH step 8c).

The DXF download is the deliverable; the DWG is a convenience for people
who work in AutoCAD.  Exporting needs the **ODA File Converter**, which
is not installed everywhere — so this module never raises for a missing
converter, it reports the reason and the DXF download stays unaffected.

The layer names are the other wrinkle: the pipeline writes ``MECH|*``,
``STR|*`` and ``QA|*``, and AutoCAD reserves ``|`` for xref-dependent
symbols.  With ``safe_layers`` (the default) every ``|`` becomes ``$``
before the export, and a name that already exists gets ``_2``, ``_3``, …
appended.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["available", "export", "DwgExportResult", "DEFAULT_VERSION",
           "UNAVAILABLE_MESSAGE"]

DEFAULT_VERSION = "R2018"

UNAVAILABLE_MESSAGE = (
    "DWG export unavailable: ODA File Converter not installed "
    "(configure ezdxf [odafc-addon] unix_exec_path / win_exec_path). "
    "The DXF download is unaffected."
)


@dataclass
class DwgExportResult:
    """What one export attempt did."""

    ok: bool = False
    path: str | None = None
    message: str = ""
    layer_renames: dict = field(default_factory=dict)


def available() -> bool:
    """Is the ODA File Converter installed?"""
    try:
        from ezdxf.addons import odafc
        return bool(odafc.is_installed())
    except Exception:
        return False


def versions() -> list[str]:
    """The DWG versions the converter can write."""
    try:
        from ezdxf.addons import odafc
        return sorted(odafc.VERSION_MAP)
    except Exception:
        return []


def _safe_layer_names(doc) -> dict:
    """Rename every layer whose name contains ``|`` to a ``$``-separated one.

    Returns ``{old_name: new_name}``.  A name that is already taken gets
    ``_2``, ``_3``, … appended, so nothing is ever overwritten.
    """
    renames: dict[str, str] = {}
    taken = {layer.dxf.name for layer in doc.layers}
    for layer in list(doc.layers):
        name = layer.dxf.name
        if "|" not in name:
            continue
        base = name.replace("|", "$")
        new = base
        n = 2
        while new in taken and new != name:
            new = f"{base}_{n}"
            n += 1
        if new == name:
            continue
        try:
            layer.rename(new)
        except Exception:
            continue
        taken.discard(name)
        taken.add(new)
        renames[name] = new
    return renames


def export(dxf_path, dwg_path, *, version: str = DEFAULT_VERSION,
           safe_layers: bool = True) -> DwgExportResult:
    """Convert ``dxf_path`` to DWG at ``dwg_path``.

    Never raises for a missing converter or a converter failure — the
    result says what happened.  An unknown ``version`` **does** raise
    ``ValueError`` (the API turns it into a 400).
    """
    from ezdxf.addons import odafc

    if version not in odafc.VERSION_MAP:
        raise ValueError(
            f"version must be one of {sorted(odafc.VERSION_MAP)}; "
            f"got {version!r}")

    if not odafc.is_installed():
        return DwgExportResult(ok=False, message=UNAVAILABLE_MESSAGE)

    from . import dxfio

    dxf_path = Path(dxf_path)
    dwg_path = Path(dwg_path)
    doc = dxfio.read_internal(str(dxf_path))

    renames = _safe_layer_names(doc) if safe_layers else {}

    dwg_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        odafc.export_dwg(doc, str(dwg_path), version=version, audit=True,
                         replace=True)
    except odafc.ODAFCError as exc:
        return DwgExportResult(ok=False,
                                message=f"the ODA File Converter failed: {exc}")
    except Exception as exc:                    # noqa: BLE001 — reported
        return DwgExportResult(
            ok=False,
            message=f"the DWG export failed: {type(exc).__name__}: {exc}")

    return DwgExportResult(ok=True, path=str(dwg_path), message="",
                            layer_renames=renames)
