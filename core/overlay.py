#!/usr/bin/env python3
"""core/overlay.py — stage 7: superimpose STR onto MECH (PLAN.md steps 6 + 7).

The STR drawing is imported into the MECH document with
:func:`ezdxf.xref.Loader` and ``ConflictPolicy.XREF_PREFIX``: every block
definition STR actually references is copied under a fresh name
(``STR$0$<name>``, or a new anonymous name) and every reference to it —
inside block definitions included — is rewritten.  Block *definitions*
are therefore never edited, and two INSERTs of the same block keep their
own insertion point, rotation and scale factors.

The imported modelspace entities are then transformed in one go by
:func:`ezdxf.transform.inplace`.  ezdxf knows how to apply a matrix to
every DXF entity type; whatever it cannot represent (typically an INSERT
under a skewed matrix) is *exploded* or *dropped* and logged.  An
untransformed entity is never left behind.

**Step 7 (this file, :func:`apply_source_layers` + :func:`draw_qa_grid`)
runs after the transform**, on the merged document: every modelspace
entity is moved onto ``MECH|<layer>`` or ``STR|<layer>`` so the two
drawings can be toggled apart in a CAD viewer, the optional
:class:`OverlayStyle` recolours them, and the QA grid of the two drawings
is drawn on ``QA|GRID`` / ``QA|GRID_STR`` / ``QA|OUTLIER``.  Inside block
definitions layer ``0`` and ``Defpoints`` are left alone so ByBlock /
ByLayer inheritance keeps working.

Reads:  cleaned MECH + cleaned STR (core.roles.resolve_pair), transform.json
Writes: out/superimposed.dxf, stages/overlay_report.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ezdxf import transform as ez_transform
from ezdxf import xref
from ezdxf.addons.importer import Importer
from ezdxf.entities import Insert
from ezdxf.tools import standards

from jobs import job_dir, load_job

from . import dxfio
from .dxfio import read_internal as _read_internal, save as _dxf_save
from .roles import resolve_pair
from .transform2d import Affine2D

__all__ = ["overlay_docs", "run", "OverlayReport", "OverlayOptions", "OverlayError",
           "OverlayStyle", "LayerReport", "apply_source_layers", "draw_qa_grid",
           "source_layer", "split_source_layer", "XREF_PREFIX", "REPORT_NAME",
           "SOURCE_LAYER_SEP", "QA_GRID_LAYER", "QA_GRID_STR_LAYER",
           "QA_OUTLIER_LAYER"]

XREF_PREFIX = "STR"      # conflict policy: imported resources become STR$0$<name>
REPORT_NAME = "overlay_report.json"


class OverlayError(RuntimeError):
    """The overlay could not be produced (or lost too much of the STR drawing)."""


# ─────────────────────────────────────────────────────────────────────────────
# Options / report
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OverlayOptions:
    """``max_drop_ratio``: share of imported entities that may be dropped.

    A similarity (the pipeline default) is exact, so the default is 0.0 —
    a single dropped entity fails the stage.  The diagnostic affine model
    tolerates a little slack.
    """

    max_drop_ratio: float = 0.0
    explode_inserts: bool = True      # INSERT fallback = explode, else drop

    @classmethod
    def for_model(cls, model: str = "similarity") -> "OverlayOptions":
        return cls(max_drop_ratio=0.01 if model == "affine" else 0.0)


@dataclass
class OverlayReport:
    """What the overlay did.  ``matrix`` is the Matrix44 actually applied.

    ``str_entity_handles`` (handles of the STR-sourced modelspace entities in
    the output) plus ``str_block_map`` / ``str_layer_map`` are what step 7
    needs to paint the two sources on their own layers.
    """

    mech_entities: int = 0
    str_entities_imported: int = 0        # STR entities present in the output
    str_entities_total: int = 0           # STR entities handed to the transform
    by_type_str: dict[str, int] = field(default_factory=dict)
    renamed_resources: dict[str, dict[str, str]] = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)
    matrix: Any = None                                  # ezdxf Matrix44
    affine: Affine2D | None = None
    str_entity_handles: list[str] = field(default_factory=list)
    str_block_map: dict[str, str] = field(default_factory=dict)
    str_layer_map: dict[str, str] = field(default_factory=dict)
    str_linetype_map: dict[str, str] = field(default_factory=dict)
    str_style_map: dict[str, str] = field(default_factory=dict)
    import_method: str = "xref"
    warnings: list[str] = field(default_factory=list)
    layers: "LayerReport | None" = None      # filled by apply_source_layers

    @property
    def dropped(self) -> int:
        return sum(1 for f in self.failures if f["action"] == "dropped")

    @property
    def exploded(self) -> int:
        return sum(1 for f in self.failures if f["action"] == "exploded")

    @property
    def drop_ratio(self) -> float:
        return (self.dropped / self.str_entities_total) if self.str_entities_total else 0.0

    def to_dict(self) -> dict:
        return {
            "mech_entities": self.mech_entities,
            "str_entities_imported": self.str_entities_imported,
            "str_entities_total": self.str_entities_total,
            "by_type_str": self.by_type_str,
            "renamed_resources": self.renamed_resources,
            "failures": self.failures,
            "matrix": self.affine.to_matrix() if self.affine else None,
            "affine": self.affine.to_json() if self.affine else None,
            "str_entity_handles": list(self.str_entity_handles),
            "str_block_map": dict(self.str_block_map),
            "str_layer_map": dict(self.str_layer_map),
            "str_linetype_map": dict(self.str_linetype_map),
            "str_style_map": dict(self.str_style_map),
            "import_method": self.import_method,
            "dropped": self.dropped,
            "exploded": self.exploded,
            "warnings": list(self.warnings),
            "style": (self.layers.style if self.layers else None),
            "source_layers": (self.layers.to_dict() if self.layers else None),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Import
# ─────────────────────────────────────────────────────────────────────────────

def _import_str(str_doc, mech_doc, report: OverlayReport) -> None:
    """Import the STR modelspace (and the blocks it references) into ``mech_doc``."""
    try:
        loader = xref.Loader(str_doc, mech_doc,
                             conflict_policy=xref.ConflictPolicy.XREF_PREFIX)
        loader.load_modelspace()
        loader.execute(xref_prefix=XREF_PREFIX)
        report.import_method = "xref"
    except Exception as exc:            # noqa: BLE001 — retried below, then reported
        report.warnings.append(
            f"xref.Loader failed ({type(exc).__name__}: {exc}); "
            "fell back to Importer.import_block(rename=True)")
        imp = Importer(str_doc, mech_doc)
        for name in [b.name for b in str_doc.blocks if not b.name.startswith("*")]:
            try:
                imp.import_block(name, rename=True)
            except Exception as block_exc:      # noqa: BLE001
                raise OverlayError(
                    f"cannot import STR block {name!r}: {block_exc}") from block_exc
        imp.import_modelspace()
        imp.finalize()
        report.import_method = "importer"


def _unprefix(name: str) -> str:
    """``STR$0$FRAME-STEEL`` -> ``FRAME-STEEL``; other names map to themselves."""
    parts = name.split("$")
    return "$".join(parts[2:]) if len(parts) >= 3 and parts[0] == XREF_PREFIX else name


def _map_table(before: set[str], table) -> dict[str, str]:
    """{source name: new name} for every entry ``table`` gained during import."""
    out: dict[str, str] = {}
    for entry in table:
        name = entry.dxf.name
        if name not in before:
            out.setdefault(_unprefix(name), name)
    return out


def _map_blocks(str_doc, mech_doc, new_entities) -> dict[str, str]:
    """{STR block name: name in mech_doc} for every block xref imported.

    Imported modelspace entities keep the order of the source entities, so
    zipping the two modelspaces pairs every source INSERT with its clone and
    yields its (possibly renamed) block name; nested INSERTs are resolved by
    descending into both block layouts.
    """
    by_name = {b.name: b for b in mech_doc.blocks}
    src_by_name = {b.name: b for b in str_doc.blocks}
    mapping: dict[str, str] = {}
    src_msp = list(str_doc.modelspace())
    queue = list(zip(src_msp, new_entities))
    if len(queue) != len(src_msp):          # a clone was lost: names only
        queue = []
    while queue:
        src, tgt = queue.pop()
        if src.dxftype() != "INSERT" or tgt.dxftype() != "INSERT":
            continue
        src_name, tgt_name = src.dxf.name, tgt.dxf.name
        if src_name in mapping:
            continue
        mapping[src_name] = tgt_name
        src_blk, tgt_blk = src_by_name.get(src_name), by_name.get(tgt_name)
        if src_blk is not None and tgt_blk is not None:
            queue.extend(zip((e for e in src_blk if e.dxftype() == "INSERT"),
                             (e for e in tgt_blk if e.dxftype() == "INSERT")))
    known = set(mapping.values())
    for name in by_name:
        if name not in known and name.startswith(f"{XREF_PREFIX}$"):
            mapping.setdefault(_unprefix(name), name)
    return mapping


# ─────────────────────────────────────────────────────────────────────────────
# Transform + fallbacks
# ─────────────────────────────────────────────────────────────────────────────

MAX_EXPLODE_DEPTH = 8


def _resolve_failures(msp, log, m, options: OverlayOptions,
                      report: OverlayReport, handles: set[str]) -> None:
    """Every logged entity is exploded (INSERT) or deleted — never left alone.

    ``explode()`` puts the *nested* INSERTs of a block into the layout as
    real entities instead of exploding them, so a piece that still fails is
    queued again: an INSERT is exploded (up to :data:`MAX_EXPLODE_DEPTH`
    levels), anything else is deleted.
    """
    queue = [(entry, 0) for entry in log]
    while queue:
        entry, depth = queue.pop(0)
        entity = entry.entity
        handle = entity.dxf.handle if entity.dxf.hasattr("handle") else None
        failure = {
            "handle": handle,
            "type": entity.dxftype(),
            "error": entry.error.name,
            "message": entry.message,
            "action": "dropped",
        }
        if entity.is_alive and isinstance(entity, Insert) and options.explode_inserts:
            try:
                pieces = list(entity.explode(msp))      # destroys the INSERT
                handles.discard(handle)
                handles.update(p.dxf.handle for p in pieces if p.is_alive)
                failure["action"] = "exploded"
                failure["n"] = len(pieces)
                # inplace() may replace a piece (CIRCLE -> ELLIPSE) and
                # destroy it, so handles are collected before, not after.
                sub_log = ez_transform.inplace(pieces, m)
                for sub in sub_log:
                    if (isinstance(sub.entity, Insert) and sub.entity.is_alive
                            and options.explode_inserts and depth < MAX_EXPLODE_DEPTH):
                        queue.append((sub, depth + 1))
                        continue
                    if sub.entity.is_alive:            # piece still fails: go away
                        sub_handle = (sub.entity.dxf.handle
                                      if sub.entity.dxf.hasattr("handle") else None)
                        msp.delete_entity(sub.entity)
                        handles.discard(sub_handle)
                    failure.setdefault("sub_errors", []).append(sub.message)
            except Exception as exc:                      # noqa: BLE001 — logged
                failure["explode_error"] = f"{type(exc).__name__}: {exc}"
        if entity.is_alive:
            msp.delete_entity(entity)
            handles.discard(handle)
        report.failures.append(failure)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def overlay_docs(mech_doc, str_doc, affine, *, options=None, style=None) -> OverlayReport:
    """Superimpose ``str_doc`` onto ``mech_doc`` by ``affine`` (STR → MECH).

    ``mech_doc`` is modified in place.  Returns the :class:`OverlayReport`.

    ``style`` (an :class:`OverlayStyle`) is optional: pass one to also run
    :func:`apply_source_layers` on the result, i.e. to get the step-7
    ``MECH|*`` / ``STR|*`` layers straight out of this call.  Without it the
    document keeps the layer names of the two source drawings (step 6
    behaviour, which is what :mod:`tests.test_overlay` asserts).
    """
    if not isinstance(affine, Affine2D):
        affine = Affine2D.from_json(affine)
    options = options or OverlayOptions()
    msp = mech_doc.modelspace()
    m = affine.to_matrix44()
    report = OverlayReport(matrix=m, affine=affine)

    mech_handles = {e.dxf.handle for e in msp}
    report.mech_entities = len(mech_handles)
    mech_blocks = {b.name for b in mech_doc.blocks}
    layer_names = {l.dxf.name for l in mech_doc.layers}
    ltype_names = {l.dxf.name for l in mech_doc.linetypes}
    style_names = {l.dxf.name for l in mech_doc.styles}

    _import_str(str_doc, mech_doc, report)

    new = [e for e in msp if e.dxf.handle not in mech_handles]
    report.str_block_map = _map_blocks(str_doc, mech_doc, new)
    report.str_layer_map = _map_table(layer_names, mech_doc.layers)
    report.str_linetype_map = _map_table(ltype_names, mech_doc.linetypes)
    report.str_style_map = _map_table(style_names, mech_doc.styles)
    report.renamed_resources = {
        "blocks": {s: n for s, n in report.str_block_map.items()
                   if s != n and n not in mech_blocks},
        "layers": {s: n for s, n in report.str_layer_map.items() if s != n},
        "linetypes": {s: n for s, n in report.str_linetype_map.items() if s != n},
        "styles": {s: n for s, n in report.str_style_map.items() if s != n},
    }

    log = ez_transform.inplace(new, m)
    handles = {e.dxf.handle for e in new}
    report.str_entities_total = len(new)
    _resolve_failures(msp, log, m, options, report, handles)

    imported = [e for e in msp if e.dxf.handle in handles]
    for e in imported:
        report.by_type_str[e.dxftype()] = report.by_type_str.get(e.dxftype(), 0) + 1
    report.str_entities_imported = len(imported)
    report.str_entity_handles = [e.dxf.handle for e in imported]

    if report.drop_ratio > options.max_drop_ratio:
        raise OverlayError(
            f"{report.dropped} of {len(new)} imported STR entities could not be "
            f"transformed and were dropped ({report.drop_ratio:.1%} > "
            f"max_drop_ratio {options.max_drop_ratio:.1%})")

    if style is not None:                  # step 7 — after the transform
        report.layers = apply_source_layers(mech_doc, report, style)
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Step 7 — per-source layers
# ─────────────────────────────────────────────────────────────────────────────
#
# Every entity of the merged document is moved onto a layer that says which
# drawing it came from:  "MECH|<layer>" / "STR|<layer>".  That is what lets
# a reviewer switch one drawing off in a CAD viewer — and what lets step 8
# count / recolour the two sources without re-deriving ownership from
# handles.  Inside block definitions layer "0" and "Defpoints" are left
# alone: an entity on "0" inherits the layer of the INSERT that pulls the
# block in, and that INSERT is itself on a "MECH|*" / "STR|*" layer.

SOURCE_LAYER_SEP = "|"     # separator of the per-source layer prefix
MECH_TAG = "MECH"
STR_TAG = "STR"
QA_TAG = "QA"
KEEP_LAYERS = ("0", "Defpoints")   # never prefixed inside blocks, never deleted
QA_GRID_LAYER = "QA|GRID"          # ACI 3 — MECH gridlines + markers + labels
QA_GRID_STR_LAYER = "QA|GRID_STR"  # ACI 6 — transformed STR gridlines + markers
QA_OUTLIER_LAYER = "QA|OUTLIER"    # ACI 1 — rejected correspondences

BYLAYER, BYBLOCK = 256, 0
_QA_CIRCLE_REL = 0.01      # circle radius / marker half-size, × median spacing
_QA_TEXT_REL = 0.005       # label text height, × median spacing
_LAYER_COPY = ("color", "true_color", "linetype", "lineweight", "plot")


def source_layer(source: str, name: str) -> str:
    """``("MECH", "FRAME-STEEL") -> "MECH|FRAME-STEEL"``."""
    return f"{source}{SOURCE_LAYER_SEP}{name}"


def split_source_layer(name: str) -> tuple[str, str] | None:
    """``"MECH|CENTER" -> ("MECH", "CENTER")``; ``None`` for any other layer."""
    head, sep, tail = name.partition(SOURCE_LAYER_SEP)
    if sep and tail and head in (MECH_TAG, STR_TAG, QA_TAG):
        return head, tail
    return None


def _is_layout_block(name: str) -> bool:
    return name.startswith("*Model_Space") or name.startswith("*Paper_Space")


def _with_attribs(layout) -> list:
    """Layout entities + the ATTRIBs of their INSERTs (not yielded by layout
    iteration, but they have a layer of their own)."""
    out = []
    for e in layout:
        out.append(e)
        if e.dxftype() == "INSERT":
            out.extend(e.attribs)
    return out


def _all_layout_entities(doc) -> list:
    """Every entity in modelspace, the paperspaces and every block layout."""
    out = []
    for layout in doc.layouts:
        out.extend(_with_attribs(layout))
    for block in doc.blocks:
        if _is_layout_block(block.name):
            continue
        out.extend(_with_attribs(block))
    return out


def _referenced_layers(doc) -> set[str]:
    """Layer names still referenced by *anything* in the document.

    A layer entry that nothing references can go (that is how the original
    ``FRAME-STEEL`` disappears once everything sits on ``STR|FRAME-STEEL``),
    but deleting one that *is* referenced would leave a dangling reference,
    so the walk also covers paperspaces, block definitions, INSERT attribs
    and the layer lists of saved views / viewports.
    """
    used: set[str] = set()

    def take(e):
        try:
            used.add(e.dxf.layer)
        except Exception:                       # noqa: BLE001 — not every entity has a layer
            return

    for entity in _all_layout_entities(doc):
        take(entity)
    for view in getattr(doc, "views", ()):
        for attr in ("frozen_layers", "plot_frozen_layers"):
            try:
                used.update(getattr(view, attr) or ())
            except Exception:                   # noqa: BLE001
                continue
    for vp in getattr(doc, "viewports", ()):
        if vp.dxf.hasattr("layer"):
            used.add(vp.dxf.layer)
    return used


def _get_layer(doc, name: str):
    """The layer table entry ``name``, or ``None`` (ezdxf's ``get`` raises)."""
    return doc.layers.get(name) if doc.layers.has_entry(name) else None


def _ensure_layer(doc, name: str, *, template=None, color=None,
                  lineweight=None, linetype=None, clear_true_color=False):
    """Create ``name`` if needed and apply the requested properties.

    ``template`` is the source layer entry whose color / true_color /
    linetype / lineweight / plot flag and on-off / freeze / lock state are
    copied (see :data:`_LAYER_COPY`).
    """
    layer = _get_layer(doc, name) or doc.layers.new(name)
    if template is not None:
        for attr in _LAYER_COPY:
            if not template.dxf.hasattr(attr):
                continue
            value = template.dxf.get(attr)
            if value is None:
                continue
            if attr == "linetype" and value not in doc.linetypes:
                value = _ensure_linetype(doc, value)     # never dangle a name
            layer.dxf.set(attr, value)
        if template.is_off():
            layer.off()
        if template.is_frozen():
            layer.freeze()
        if template.is_locked():
            layer.lock()
    if color is not None:
        layer.dxf.color = int(color)
        if clear_true_color and layer.dxf.hasattr("true_color"):
            del layer.dxf.true_color
    if lineweight is not None:
        layer.dxf.lineweight = int(lineweight)
    if linetype is not None:
        layer.dxf.linetype = linetype
    return layer


def _ensure_linetype(doc, name: str) -> tuple[str, bool]:
    """Make sure ``name`` exists in ``doc.linetypes``.

    Returns ``(name, known)``.  ezdxf ships the ISO linetype set
    (:func:`ezdxf.tools.standards.linetypes`); a name from that set is
    imported with its real pattern.  Anything else becomes a plain dashed
    pattern — better a linetype that is not the one asked for than a layer
    referencing a missing table entry.
    """
    if name in doc.linetypes:
        return name, True
    for std_name, description, pattern in standards.linetypes():
        if std_name.lower() == name.lower():
            doc.linetypes.add(std_name, pattern=pattern, description=description)
            return std_name, True
    doc.linetypes.add(name, pattern=[0.5, -0.25],
                      description=f"{name} (added by the overlay stage)")
    return name, False


def _to_bylayer(e) -> None:
    """Entity colour → BYLAYER (BYBLOCK entities are left alone)."""
    if not e.is_supported_dxf_attrib("color"):
        return
    if e.dxf.hasattr("color") and e.dxf.color == BYBLOCK:
        return
    e.dxf.color = BYLAYER
    if e.dxf.hasattr("true_color"):
        del e.dxf.true_color


# ─────────────────────────────────────────────────────────────────────────────
# OverlayStyle
# ─────────────────────────────────────────────────────────────────────────────

def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off", ""):
            return False
    return default


def _as_int(value):
    try:
        if isinstance(value, str) and not value.strip():
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class OverlayStyle:
    """How the two drawings are painted in the superimposed file.

    ``color_mode``
        ``"original"`` keeps every layer's own colour / linetype /
        lineweight (the prefixed copy of the source layer entry);
        ``"contrast"`` forces ``MECH|*`` to ``mech_color`` and ``STR|*`` to
        ``str_color`` and sets every entity of that drawing to BYLAYER so
        the layer colour wins.
    ``str_lineweight`` / ``str_linetype``
        Applied to the ``STR|*`` layers in **both** modes — they are an
        explicit request, not part of the colour mode.  ``str_linetype`` is
        added to the LTYPE table when the drawing does not have it.
    ``qa_grid``
        Draw the QA grid (see :func:`draw_qa_grid`).
    """

    color_mode: str = "original"
    mech_color: int = 5
    str_color: int = 1
    str_lineweight: int | None = None
    str_linetype: str | None = None
    qa_grid: bool = True

    COLOR_MODES = ("original", "contrast")

    def to_dict(self) -> dict:
        return {
            "color_mode": self.color_mode,
            "mech_color": self.mech_color,
            "str_color": self.str_color,
            "str_lineweight": self.str_lineweight,
            "str_linetype": self.str_linetype,
            "qa_grid": self.qa_grid,
        }

    @classmethod
    def from_options(cls, raw) -> "OverlayStyle":
        """Build from a ``job.options`` dict (or just its ``overlay`` part).

        Options may live under ``options["overlay"]`` (what the API writes)
        or flat in ``options`` itself; unknown keys and unusable values are
        ignored rather than failing the stage.
        """
        style = cls()
        if not isinstance(raw, dict):
            return style
        opts = dict(raw)
        sub = opts.get("overlay")
        if isinstance(sub, dict):
            opts = {k: v for k, v in opts.items() if k != "overlay"} | sub

        mode = opts.get("color_mode", opts.get("overlay_color_mode"))
        if isinstance(mode, str) and mode.strip().lower() in cls.COLOR_MODES:
            style.color_mode = mode.strip().lower()
        for key, attr in (("mech_color", "mech_color"), ("str_color", "str_color")):
            val = _as_int(opts.get(key))
            if val is not None:
                setattr(style, attr, val)
        lw = _as_int(opts.get("str_lineweight", opts.get("str_line_weight")))
        if lw is not None and 0 <= lw <= 211:      # DXF lineweights are 1/100 mm
            style.str_lineweight = lw
        lt = opts.get("str_linetype", opts.get("str_line_type"))
        if isinstance(lt, str) and lt.strip():
            style.str_linetype = lt.strip()
        if "qa_grid" in opts:
            style.qa_grid = _as_bool(opts.get("qa_grid"), style.qa_grid)
        return style

    @classmethod
    def from_job(cls, job) -> "OverlayStyle":
        return cls.from_options(getattr(job, "options", None))


@dataclass
class LayerReport:
    """What :func:`apply_source_layers` did.

    ``mech_layers`` / ``str_layers`` map the layer name in the merged
    document *before* the call to its ``MECH|*`` / ``STR|*`` counterpart,
    ``deleted_layers`` are the original entries nothing referenced any more.
    """

    mech_layers: dict = field(default_factory=dict)
    str_layers: dict = field(default_factory=dict)
    deleted_layers: list = field(default_factory=list)
    kept_layers: list = field(default_factory=list)
    entities: dict = field(default_factory=dict)
    clayer: str = "0"
    style: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mech_layers": dict(self.mech_layers),
            "str_layers": dict(self.str_layers),
            "deleted_layers": list(self.deleted_layers),
            "kept_layers": list(self.kept_layers),
            "entities": dict(self.entities),
            "clayer": self.clayer,
            "style": dict(self.style),
            "warnings": list(self.warnings),
        }


def _str_block_names(doc, report: OverlayReport) -> set[str]:
    """Block definitions that belong to the STR drawing in the output.

    ``report.str_block_map`` (source name → name in the merged doc) is the
    primary source; walking the STR-sourced INSERTs covers a block whose
    clone kept its original name, which the map may not have caught.
    """
    names = set(report.str_block_map.values())
    seen = set()
    queue = [e for e in doc.modelspace()
             if e.dxf.handle in set(report.str_entity_handles)
             and e.dxftype() == "INSERT"]
    while queue:
        ins = queue.pop()
        name = ins.dxf.name
        names.add(name)
        if name in seen or name not in doc.blocks:
            continue
        seen.add(name)
        queue.extend(e for e in doc.blocks.get(name) if e.dxftype() == "INSERT")
    return names


def apply_source_layers(doc, report: OverlayReport, style: OverlayStyle | None = None
                        ) -> LayerReport:
    """Move every entity of the merged ``doc`` onto ``MECH|*`` / ``STR|*``.

    Ownership comes from :class:`OverlayReport` — the handles of the STR
    modelspace entities and the block names xref copied in.  Layer ``0`` and
    ``Defpoints`` keep their name inside a block definition so ByLayer /
    ByBlock inheritance still resolves to the INSERT's own ``MECH|*`` /
    ``STR|*`` layer; in modelspace (and therefore also for the blocks pulled
    in from there) layer ``0`` *is* renamed to ``MECH|0`` / ``STR|0``.

    The original layer entries are deleted once nothing references them any
    more (never ``0`` or ``Defpoints``), and ``$CLAYER`` is reset to ``0``.

    Runs **after** the transform — the layer names are the ones the merged
    document has then, and ``draw_qa_grid`` (which owns the ``QA|*`` layers)
    runs after this.
    """
    style = style or OverlayStyle()
    out = LayerReport(style=style.to_dict())
    msp = doc.modelspace()
    str_handles = set(report.str_entity_handles)
    str_blocks = _str_block_names(doc, report)

    # ── 1. sort every entity into a source ────────────────────────────────
    mech_msp: list = []
    str_msp: list = []
    for e in msp:
        bucket = str_msp if e.dxf.handle in str_handles else mech_msp
        bucket.append(e)
        if e.dxftype() == "INSERT":
            bucket.extend(e.attribs)          # an ATTRIB belongs to its INSERT

    shared = str_blocks & {e.dxf.name for e in mech_msp if e.dxftype() == "INSERT"}
    if shared:
        out.warnings.append(
            f"{len(shared)} block definition(s) are INSERTed by both drawings "
            f"({', '.join(sorted(shared))}); their inner layers are left alone "
            "because prefixing them would recolour the MECH usage as well")

    mech_block: list = []
    str_block: list = []
    for block in doc.blocks:
        if _is_layout_block(block.name):
            continue
        ents = _with_attribs(block)
        if block.name in str_blocks and block.name not in shared:
            str_block.extend(ents)
        else:
            mech_block.extend(ents)

    # ── 2. plan the renames ───────────────────────────────────────────────
    mapping: dict[tuple[str, str], str] = {}
    plan: list[tuple[Any, str]] = []

    def add(entities, source: str, *, in_block: bool) -> None:
        for e in entities:
            layer = e.dxf.layer
            if in_block and layer in KEEP_LAYERS:
                continue                     # ByBlock / ByLayer inheritance
            key = (source, layer)
            if key not in mapping:
                shown = _unprefix(layer) if source == STR_TAG else layer
                mapping[key] = source_layer(source, shown)
            plan.append((e, mapping[key]))

    add(mech_msp, MECH_TAG, in_block=False)
    add(str_msp, STR_TAG, in_block=False)
    add(mech_block, MECH_TAG, in_block=True)
    add(str_block, STR_TAG, in_block=True)

    # ── 3. create the prefixed layers, copying the source layer's look ────
    for (source, original), new_name in sorted(mapping.items()):
        template = _get_layer(doc, original)
        _ensure_layer(doc, new_name, template=template)
        (out.mech_layers if source == MECH_TAG else out.str_layers)[original] = new_name
    for e, new_name in plan:
        e.dxf.layer = new_name

    # ── 4. styling ───────────────────────────────────────────────────────
    if style.color_mode == "contrast":
        for original, new_name in out.mech_layers.items():
            _ensure_layer(doc, new_name, color=style.mech_color,
                          clear_true_color=True)
        for original, new_name in out.str_layers.items():
            _ensure_layer(doc, new_name, color=style.str_color,
                          clear_true_color=True)
        for e in list(mech_msp) + mech_block:
            _to_bylayer(e)
        for e in list(str_msp) + str_block:
            _to_bylayer(e)

    if style.str_lineweight is not None:
        for new_name in out.str_layers.values():
            _ensure_layer(doc, new_name, lineweight=style.str_lineweight)
    if style.str_linetype:
        ltype, known = _ensure_linetype(doc, style.str_linetype)
        for new_name in out.str_layers.values():
            _ensure_layer(doc, new_name, linetype=ltype)
        if not known:
            out.warnings.append(
                f"{style.str_linetype!r} is not one of the standard linetypes; "
                "a plain dashed pattern was added for it")

    # ── 5. drop the original layer entries nothing points at any more ────
    used = _referenced_layers(doc)
    for layer in list(doc.layers):
        name = layer.dxf.name
        if name in used or name in KEEP_LAYERS or split_source_layer(name):
            continue
        doc.layers.remove(name)
        out.deleted_layers.append(name)
    out.kept_layers = [l.dxf.name for l in doc.layers if l.dxf.name in KEEP_LAYERS]

    # ── 6. the file opens on layer 0 ─────────────────────────────────────
    doc.header["$CLAYER"] = "0"
    out.clayer = "0"
    out.entities = {"mech": len(mech_msp), "str": len(str_msp),
                    "mech_block": len(mech_block), "str_block": len(str_block)}
    report.warnings.extend(out.warnings)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Step 7 — the QA grid
# ─────────────────────────────────────────────────────────────────────────────

def _load_json(obj):
    """Accept a parsed dict or a path to a JSON file."""
    if isinstance(obj, (str, Path)):
        return json.loads(Path(obj).read_text())
    return obj


def _median(values) -> float:
    v = sorted(float(x) for x in values)
    n = len(v)
    if not n:
        return 0.0
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


def _median_spacing(grid_json: dict) -> float:
    """Median distance between adjacent parallel gridlines, in the units of
    the grid — the same number ``fit`` uses for its tolerances."""
    axes = {k: v for k, v in (grid_json.get("axes") or {}).items()
            if (v or {}).get("status") == "ok" and v.get("point") and v.get("dir")}
    per_family = []
    for fam in grid_json.get("families") or []:
        members = [axes[k] for k in fam if k in axes]
        if len(members) < 2:
            continue
        dx, dy = members[0]["dir"]
        bx, by = members[0]["point"]
        nx, ny = -dy, dx                       # offset along the family's normal
        ts = sorted((a["point"][0] - bx) * nx + (a["point"][1] - by) * ny
                    for a in members)
        gaps = [b - a for a, b in zip(ts, ts[1:]) if b - a > 0.0]
        if gaps:
            per_family.append(_median(gaps))
    if per_family:
        return _median(per_family)
    return float(grid_json.get("extent") or 0.0)


def _x_marker(msp, layer, x, y, size) -> int:
    """Two crossing LINEs centred on (x, y) — the STR intersection mark."""
    msp.add_line((x - size, y - size), (x + size, y + size), dxfattribs={"layer": layer})
    msp.add_line((x - size, y + size), (x + size, y - size), dxfattribs={"layer": layer})
    return 2


def draw_qa_grid(doc, transform_json, grid_mech, grid_str, *, spacing=None) -> dict:
    """Draw the QA grid of both drawings into ``doc``.

    ``transform_json`` is ``transform.json`` (its ``matrix``,
    ``correspondences`` and ``outliers`` are used), ``grid_mech`` /
    ``grid_str`` are the ``grid_mech.json`` / ``grid_str.json`` sidecars
    written by :mod:`core.fit`.  Dicts or paths are both accepted.

    What lands where:

    * the MECH gridlines as LINEs on ``QA|GRID`` (ACI 3),
    * the STR gridlines, mapped through the fitted transform, on
      ``QA|GRID_STR`` (ACI 6) — they should land exactly on the MECH ones,
    * a CIRCLE at every MECH intersection and an X at every transformed STR
      intersection, radius / half-size ``0.01 × median grid spacing``,
    * ``"{label} r={residual:.3g}"`` TEXT at every correspondence,
      height ``0.005 × median spacing``,
    * an X on ``QA|OUTLIER`` (ACI 1) at every intersection the fit rejected.

    Only ``QA|*`` layers are touched, so it never disturbs the
    ``MECH|*`` / ``STR|*`` prefixing.  Returns a small summary dict.
    """
    transform_json = _load_json(transform_json) or {}
    grid_mech = _load_json(grid_mech) or {}
    grid_str = _load_json(grid_str) or {}
    if not transform_json:
        raise OverlayError("draw_qa_grid needs the fit output (transform.json)")

    msp = doc.modelspace()
    _ensure_layer(doc, QA_GRID_LAYER, color=3)
    _ensure_layer(doc, QA_GRID_STR_LAYER, color=6)
    _ensure_layer(doc, QA_OUTLIER_LAYER, color=1)

    affine = Affine2D.from_json(transform_json)
    sp = float(spacing or transform_json.get("median_spacing")
               or _median_spacing(grid_mech) or 0.0)
    if not sp > 0.0:
        raise OverlayError(
            "cannot size the QA grid: no median grid spacing in transform.json "
            "or grid_mech.json")
    r = _QA_CIRCLE_REL * sp
    h = _QA_TEXT_REL * sp

    def axis_lines(grid_json, layer, to_world):
        n = 0
        for label, ax in (grid_json.get("axes") or {}).items():
            ax = ax or {}
            if ax.get("status") != "ok" or not ax.get("point") or not ax.get("dir"):
                continue
            p = ax["point"]
            length = float(ax.get("length") or 0.0)
            if length <= 0.0:
                continue
            d = ax["dir"]
            a = to_world((float(p[0]), float(p[1])))
            b = to_world((float(p[0]) + float(d[0]) * length,
                          float(p[1]) + float(d[1]) * length))
            msp.add_line(a, b, dxfattribs={"layer": layer})
            n += 1
        return n

    def marks(intersections, layer, to_world, *, circle):
        n = 0
        for label, pt in (intersections or {}).items():
            if not pt or len(pt) < 2:
                continue
            x, y = to_world((float(pt[0]), float(pt[1])))
            if circle:
                msp.add_circle((x, y), r, dxfattribs={"layer": layer})
                n += 1
            else:
                n += _x_marker(msp, layer, x, y, r)
        return n

    mech_axes = axis_lines(grid_mech, QA_GRID_LAYER, lambda p: p)
    str_axes = axis_lines(grid_str, QA_GRID_STR_LAYER, affine.apply)
    mech_marks = marks(grid_mech.get("intersections"), QA_GRID_LAYER,
                       lambda p: p, circle=True)
    str_marks = marks(grid_str.get("intersections"), QA_GRID_STR_LAYER,
                      affine.apply, circle=False)

    correspondences = transform_json.get("correspondences") or []
    n_labels = 0
    for c in correspondences:
        pt = c.get("mech") or c.get("predicted")
        if not pt or len(pt) < 2:
            continue
        residual = float(c.get("residual") or 0.0)
        msp.add_text(
            f"{c.get('label', '?')} r={residual:.3g}",
            dxfattribs={"insert": (float(pt[0]) + r, float(pt[1]) + r),
                        "height": h, "layer": QA_GRID_LAYER})
        n_labels += 1

    mech_ix = grid_mech.get("intersections") or {}
    by_label = {c.get("label"): c for c in correspondences}
    outliers = list(transform_json.get("outliers") or [])
    if not outliers:
        outliers = [c.get("label") for c in correspondences if not c.get("inlier", True)]
    n_outliers = 0
    for label in outliers:
        pt = None
        if label in mech_ix:
            pt = mech_ix[label]
        elif label in by_label:
            pt = by_label[label].get("mech")
        if not pt or len(pt) < 2:
            continue
        _x_marker(msp, QA_OUTLIER_LAYER, float(pt[0]), float(pt[1]), r)
        n_outliers += 1

    return {
        "median_spacing": sp,
        "mech_axes": mech_axes,
        "str_axes": str_axes,
        "mech_intersections": mech_marks,
        "str_intersections": str_marks,
        "labels": n_labels,
        "outliers": n_outliers,
        "layers": [QA_GRID_LAYER, QA_GRID_STR_LAYER, QA_OUTLIER_LAYER],
    }


def run(job_id: str, *, pair_dir: Path | None = None,
        options: OverlayOptions | None = None,
        style: OverlayStyle | None = None) -> None:
    """Stage 7 entrypoint — writes ``out/superimposed.dxf`` and the report.

    ``pair_dir`` is the per-pair ``cleaned/`` directory of a multi-pair run;
    every output then goes to the pair root instead of the job dir.

    After the transform the document is put on per-source layers and the QA
    grid is drawn (both configurable through ``job.options["overlay"]``).
    """
    d = job_dir(job_id)
    root = Path(pair_dir).parent if pair_dir is not None else d
    mech_path, str_path = resolve_pair(job_id, pair_dir)   # explicit roles

    transform_path = root / "transform.json"
    if not transform_path.exists():
        raise RuntimeError("transform.json missing — run fit first")
    transform_json = json.loads(transform_path.read_text())
    affine = Affine2D.from_json(transform_json)

    mech_doc = _read_internal(str(mech_path))
    report = overlay_docs(mech_doc, _read_internal(str(str_path)), affine,
                          options=options)

    style = style or OverlayStyle.from_job(load_job(job_id))
    report.layers = apply_source_layers(mech_doc, report, style)

    qa = None
    if style.qa_grid:
        grid_mech, grid_str = root / "grid_mech.json", root / "grid_str.json"
        if grid_mech.exists() and grid_str.exists():
            qa = draw_qa_grid(mech_doc, transform_json, grid_mech, grid_str)
        else:
            report.warnings.append(
                "qa_grid was requested but grid_mech.json / grid_str.json are "
                "missing (no fit stage output for this pair) — no QA geometry "
                "was drawn")

    out_dir = root / "out"
    out_dir.mkdir(exist_ok=True)
    summary = _dxf_save(mech_doc, str(out_dir / "superimposed.dxf"))

    stages = root / "stages"
    stages.mkdir(exist_ok=True)
    payload = report.to_dict()
    payload["qa_grid"] = qa
    (stages / REPORT_NAME).write_text(json.dumps(payload, indent=2))
    dxfio.record_audit(root, "overlay", Path(mech_path).name, summary)