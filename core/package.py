#!/usr/bin/env python3
"""core/package.py — Stage 8 (PLAN.md step 8).

Renders previews of the superimposed drawing and writes one JSON report
describing the whole run.  The DXF produced by stage 7 is left untouched —
the recolouring the preview needs happens in memory only.

Three things this module refuses to do:

* **hide geometry.**  The preview is drawn on the *full* extents of the
  drawing.  The percentile/IQR crop this replaced made misplaced entities
  disappear from the picture, which is exactly when a reviewer has to see
  them.  Entities that sit far outside the grid box are *reported*
  (:func:`_find_outliers`), never cropped away.
* **drop entities quietly.**  :func:`_drop_dangling_inserts` returns what it
  removed and the count lands in ``report.json``.
* **guess roles from names.**  A source comes from the step-7 layer prefix
  (:func:`core.overlay.split_source_layer`) or, failing that, from the
  manifest (:mod:`core.roles`) — never from a filename.

Reads:  jobs/<id>/out/superimposed.dxf, the cleaned MECH/STR pair,
        transform.json, stages/overlay_report.json, stages/audit.json
Writes: jobs/<id>/out/preview.png          — the full extents
        jobs/<id>/out/preview_grid.png     — the grid box + 10%
        jobs/<id>/out/preview_meta.json    — world → pixel map for the UI
        jobs/<id>/out/report.json
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from ezdxf import bbox as ez_bbox
from ezdxf import disassemble
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing import config as draw_config
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

from jobs import job_dir
from . import dxfio
from . import overlay
from . import roles as roles_mod
from .overlay import split_source_layer
from .transform2d import Affine2D

# ─────────────────────────────────────────────────────────────────────────────
# constants
# ─────────────────────────────────────────────────────────────────────────────

MECH_TAG = overlay.MECH_TAG          # "MECH"
STR_TAG = overlay.STR_TAG            # "STR"
QA_TAG = overlay.QA_TAG              # "QA"
QA_GEOMETRY = ("GRID", "GRID_STR", "OUTLIER")       # QA| layers this stage draws
MISMATCH_PREFIX = f"{QA_TAG}{overlay.SOURCE_LAYER_SEP}MISMATCH"   # step 8b owns those

#: ACI colours the preview paints with (ACI 8 grey / 1 red / 3 green).
PREVIEW_COLOR = {MECH_TAG: 8, STR_TAG: 1, QA_TAG: 3}

PAD = 0.05                  # extents padding, fraction of the span
GRID_PAD = 0.10             # ... and of the grid-cropped preview
OUTLIER_DIAGONALS = 10.0    # an entity this far off the grid box is an outlier
MAX_OUTLIERS = 200          # reported per run (the count is always exact)
MAX_DANGLING = 200

PREVIEW_PNG = "preview.png"
PREVIEW_GRID_PNG = "preview_grid.png"
PREVIEW_META = "preview_meta.json"
REPORT_JSON = "report.json"


# ─────────────────────────────────────────────────────────────────────────────
# fonts (unchanged — a missing font must never abort the preview)
# ─────────────────────────────────────────────────────────────────────────────

def _find_usable_font() -> str | None:
    """Return path to a font file, or None.

    Tries:
      1. matplotlib's own bundled fonts (DejaVu Sans)
      2. Common Linux font paths (/usr/share/fonts/...)
    Returns None if nothing found.
    """
    import os
    candidates = [
        os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf", "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/noto/NotoSans-Regular.ttf",
        # Anything Noto/DejaVu as last resort
        "/usr/share/fonts/TTF/DejaVuSerif.ttf",
        "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _ensure_matplotlib_has_fonts() -> None:
    """Force matplotlib to (re)discover fonts on disk and pick a default.

    Workaround for systems where matplotlib's first cache build happens
    before any font is registered, leading to "no fonts available" at
    render time.
    """
    import warnings
    warnings.filterwarnings("ignore", message=".*font.*")
    try:
        import matplotlib.font_manager as fm
        # Clear cache and rebuild
        try:
            cache = fm.get_cachedir()
        except Exception:
            cache = None
        try:
            fm._load_fontmanager(try_read_cache=False)
        except Exception:
            pass
        # If matplotlib still can't find fonts, register DejaVu manually
        font_path = _find_usable_font()
        if font_path:
            try:
                fm.fontManager.addfont(font_path)
            except Exception:
                pass
            matplotlib.rcParams["font.family"] = fm.FontProperties(fname=font_path).get_name()
        else:
            # Nothing on disk that matplotlib can recognize — last-ditch
            # try forcing the default family.
            matplotlib.rcParams["font.family"] = "sans-serif"
            matplotlib.rcParams["font.sans-serif"] = ["DejaVu Sans"]
    except Exception:
        pass


def _patch_font_cache() -> None:
    """Keep ezdxf's font cache from aborting a render on a missing font.

    Without this the ``complex_mtext_renderer`` (used for MTEXT) and the
    standard text renderer raise ``AttributeError: 'NoneType' object has no
    attribute 'filename'`` when no font was registered on disk.  The cache
    lookup is wrapped to answer with a placeholder FontFace: the renderer
    then skips glyph generation and the geometry keeps drawing.
    """
    try:
        from ezdxf.fonts import font_manager as _fm
        from ezdxf.fonts.fonts import FontFace as _FontFace
        _CacheClass = getattr(_fm, "FontCache", None) or getattr(_fm, "FontFaceCache", None)
        if _CacheClass is None:
            return
        _orig_best_match = _CacheClass.find_best_match

        def _safe_find_best_match(self, font_face):
            try:
                if font_face is None or getattr(font_face, "filename", None) is None:
                    placeholder = _FontFace()
                    placeholder.filename = "_no_font_"
                    return placeholder
                return _orig_best_match(self, font_face)
            except AttributeError:
                placeholder = _FontFace()
                placeholder.filename = "_no_font_"
                return placeholder

        _CacheClass.find_best_match = _safe_find_best_match
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# extents
# ─────────────────────────────────────────────────────────────────────────────

def _entity_extent(e) -> tuple[float, float, float, float] | None:
    """Return (xmin, ymin, xmax, ymax) for an entity, or None.

    Fallback for entities ezdxf cannot produce a bounding box for.  The real
    extents come from :func:`ezdxf.bbox` (recursive through INSERTs).
    """
    try:
        bb = e.bbox()
        if bb is not None:
            return (float(bb.extmin[0]), float(bb.extmin[1]),
                    float(bb.extmax[0]), float(bb.extmax[1]))
    except Exception:
        pass
    t = e.dxftype()
    try:
        if t in ("LINE", "RAY", "XLINE"):
            s = e.dxf.start
            e2 = e.dxf.end
            return (min(s[0], e2[0]), min(s[1], e2[1]),
                    max(s[0], e2[0]), max(s[1], e2[1]))
        if t == "LWPOLYLINE":
            verts = list(e.vertices())
            if not verts:
                return None
            xs = [v[0] for v in verts]
            ys = [v[1] for v in verts]
            return (min(xs), min(ys), max(xs), max(ys))
        if t in ("CIRCLE", "ARC"):
            c = e.dxf.center
            r = float(e.dxf.radius)
            return (c[0] - r, c[1] - r, c[0] + r, c[1] + r)
        if t in ("TEXT", "MTEXT", "INSERT"):
            p = e.dxf.insert
            return (float(p[0]), float(p[1]), float(p[0]), float(p[1]))
        if t == "POINT":
            p = e.dxf.location
            return (float(p[0]), float(p[1]), float(p[0]), float(p[1]))
    except Exception:
        return None
    return None


class _Accum:
    """Running (xmin, ymin, xmax, ymax) of a set of bounding boxes."""

    __slots__ = ("xmin", "ymin", "xmax", "ymax", "n")

    def __init__(self):
        self.xmin = self.ymin = math.inf
        self.xmax = self.ymax = -math.inf
        self.n = 0

    def add(self, box) -> None:
        self.xmin = min(self.xmin, box[0])
        self.ymin = min(self.ymin, box[1])
        self.xmax = max(self.xmax, box[2])
        self.ymax = max(self.ymax, box[3])
        self.n += 1

    def raw(self) -> tuple[float, float, float, float] | None:
        if self.n == 0:
            return None
        return (self.xmin, self.ymin, self.xmax, self.ymax)


def _as_box(bb) -> tuple[float, float, float, float]:
    return (float(bb.extmin[0]), float(bb.extmin[1]),
            float(bb.extmax[0]), float(bb.extmax[1]))


def _pad(box: tuple[float, float, float, float] | None, frac: float = PAD,
         ) -> tuple[float, float, float, float] | None:
    """Grow ``box`` by ``frac`` of its span on every side (min. 1 unit)."""
    if box is None:
        return None
    x0, y0, x1, y1 = box
    px = max((x1 - x0) * frac, 1.0)
    py = max((y1 - y0) * frac, 1.0)
    return (x0 - px, y0 - py, x1 + px, y1 + py)


def _diagonal(box) -> float:
    if box is None:
        return 0.0
    return math.hypot(box[2] - box[0], box[3] - box[1])


def _box_distance(a, b) -> float:
    """Distance between two axis-aligned boxes (0 when they overlap)."""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def _source_of(entity) -> str | None:
    """``"MECH"`` / ``"STR"`` / ``"QA"`` from a step-7 layer prefix, else None."""
    try:
        layer = entity.dxf.layer
    except Exception:
        return None
    split = split_source_layer(layer)
    return split[0] if split else None


@dataclass
class Extents:
    """The boxes step 8 measures, plus the geometry it refuses to crop.

    ``full``
        Everything in modelspace, ``QA|*`` included, padded by 5%.  This is
        what ``preview.png`` shows — nothing is cropped away.
    ``mech`` / ``str``
        The same, restricted to one source.  ``None`` when a source is absent.
    ``grid``
        The drawing without the QA grid this stage itself drew (otherwise the
        QA geometry would define the box around it).  ``preview_grid.png`` is
        this box + 10%.
    ``outliers``
        Entities further than :data:`OUTLIER_DIAGONALS` MECH diagonals from
        the MECH box, farthest first.
    """

    full: tuple | None = None
    mech: tuple | None = None
    str_: tuple | None = None
    grid: tuple | None = None
    outliers: list = field(default_factory=list)
    outlier_total: int = 0
    entity_boxes: int = 0

    def to_dict(self) -> dict:
        return {"full": list(self.full) if self.full else None,
                "mech": list(self.mech) if self.mech else None,
                "str": list(self.str_) if self.str_ else None,
                "grid": list(self.grid) if self.grid else None}


def _find_outliers(items, ref_box, full_box, *, limit: int = MAX_OUTLIERS
                   ) -> tuple[list[dict], int]:
    """Entities further than :data:`OUTLIER_DIAGONALS` grid diagonals out.

    The reference is the **MECH** box — the drawing the grid belongs to, so a
    stray STR entity is measured against the drawing it was fitted into (with
    no MECH geometry at all, against the non-QA drawing box instead).

    Reported, never hidden.  Each record carries the handle, layer, source and
    type of the *modelspace* entity the box belongs to (for an INSERT that is
    the INSERT — the misplaced content is inside its block), plus
    ``fraction_of_extents``: the gap to the reference box as a fraction of the
    full extents diagonal, and ``grid_diagonals``: the same gap in grid
    diagonals.
    """
    if ref_box is None:
        return [], 0
    diag = _diagonal(ref_box)
    if diag <= 0.0:
        return [], 0
    limit_d = OUTLIER_DIAGONALS * diag
    full_diag = _diagonal(full_box) or diag
    found: list[dict] = []
    for entity, box, source in items:
        d = _box_distance(box, ref_box)
        if d <= limit_d:
            continue
        try:
            handle = entity.dxf.handle
        except Exception:
            handle = None
        try:
            layer = entity.dxf.layer
        except Exception:
            layer = ""
        found.append({
            "handle": handle,
            "type": entity.dxftype(),
            "layer": layer,
            "source": source,
            "bbox": [float(v) for v in box],
            "distance": d,
            "fraction_of_extents": d / full_diag,
            "grid_diagonals": d / diag,
        })
    found.sort(key=lambda r: (-r["distance"], r["handle"] or ""))
    return found[:limit], len(found)


def _collect_extents(doc) -> tuple[Extents, dict]:
    """Measure the drawing in one pass.

    Walks modelspace, expands every INSERT (non-fast bbox, so Béziers and
    splines contribute real points) and groups the result by layer prefix.
    Returns ``(Extents, {handle: box})`` — the second value is the per-entity
    world box the renderer uses to skip what is outside the view.
    """
    groups = {k: _Accum() for k in ("full", MECH_TAG, STR_TAG, "grid")}
    cache = ez_bbox.Cache()
    items: list[tuple] = []
    by_handle: dict = {}
    for e in doc.modelspace():
        source = _source_of(e)
        try:
            boxes = [_as_box(bb) for bb in
                     ez_bbox.multi_flat([e], fast=False, cache=cache)]
        except Exception:
            # An entity ezdxf cannot size (a broken INSERT, an ACAD proxy)
            # still has to be visible in the preview and in the counts.
            fallback = _entity_extent(e)
            boxes = [fallback] if fallback is not None else []
        for box in boxes:
            groups["full"].add(box)
            if source == MECH_TAG:
                groups[MECH_TAG].add(box)
            elif source == STR_TAG:
                groups[STR_TAG].add(box)
            if source != QA_TAG:                # the QA grid is not the drawing
                groups["grid"].add(box)
            items.append((e, box, source))
            try:
                by_handle.setdefault(e.dxf.handle, box)
            except Exception:
                pass

    full = groups["full"].raw()
    grid = groups["grid"].raw()
    mech = groups[MECH_TAG].raw()
    outliers, n_outliers = _find_outliers(items, mech or grid, full)
    ext = Extents(
        full=_pad(full),
        mech=_pad(mech),
        str_=_pad(groups[STR_TAG].raw()),
        grid=_pad(grid),
        outliers=outliers,
        outlier_total=n_outliers,
        entity_boxes=len(items),
    )
    return ext, by_handle


def _compute_extents(doc) -> tuple[float, float, float, float] | None:
    """``(xmin, ymin, xmax, ymax)`` of the whole drawing, padded by 5%.

    Full extents, recursive through INSERTs — a percentile/IQR crop used to
    sit here and hid every misplaced entity.  Stray geometry is reported by
    :func:`_collect_extents` as ``outliers`` instead.
    """
    return _collect_extents(doc)[0].full


# ─────────────────────────────────────────────────────────────────────────────
# dangling INSERTs
# ─────────────────────────────────────────────────────────────────────────────

def _drop_dangling_inserts(doc) -> list[dict]:
    """Remove INSERT entities that reference a non-existent block def.

    The Frontend renderer raises "Required block definition for ... does not
    exist." on the first dangling reference and aborts the whole preview.  We
    drop them so the preview renders — and *report* every one of them instead
    of losing it silently (see ``report.json``).

    Returns one record per dropped INSERT.
    """
    block_names = {b.name for b in doc.blocks}
    dropped: list[dict] = []
    for layout in [doc.modelspace()] + [b for b in doc.blocks if not b.name.startswith("*")]:
        for e in list(layout):
            try:
                if e.dxftype() != "INSERT":
                    continue
                if e.dxf.name in block_names:
                    continue
            except Exception:
                continue
            record = {
                "handle": e.dxf.handle if e.dxf.hasattr("handle") else None,
                "block": e.dxf.name if e.dxf.hasattr("name") else None,
                "layer": e.dxf.layer if e.dxf.hasattr("layer") else None,
                "layout": layout.name if hasattr(layout, "name") else None,
            }
            try:
                layout.delete_entity(e)
            except Exception:
                continue
            dropped.append(record)
    return dropped


# ─────────────────────────────────────────────────────────────────────────────
# preview
# ─────────────────────────────────────────────────────────────────────────────

def _iter_all_entities(doc):
    """Modelspace, paperspaces and every block layout, INSERT attribs included."""
    for layout in doc.layouts:
        for e in layout:
            yield e
            if e.dxftype() == "INSERT":
                yield from e.attribs
    for block in doc.blocks:
        name = block.name
        if name.startswith("*Model_Space") or name.startswith("*Paper_Space"):
            continue
        for e in block:
            yield e
            if e.dxftype() == "INSERT":
                yield from e.attribs


def _set_bylayer(e) -> None:
    """Entity colour → BYLAYER so the layer colour wins.  BYBLOCK stays BYBLOCK."""
    if not e.is_supported_dxf_attrib("color"):
        return
    if e.dxf.hasattr("color") and e.dxf.color == overlay.BYBLOCK:
        return
    e.dxf.color = overlay.BYLAYER
    if e.dxf.hasattr("true_color"):
        del e.dxf.true_color


def _recolor_for_preview(doc) -> dict:
    """Paint the two drawings apart *in memory* (the document is not saved).

    ``MECH|*`` grey (ACI 8), ``STR|*`` red (ACI 1), the ``QA|GRID*`` /
    ``QA|OUTLIER`` layers green (ACI 3).  Everything else keeps its own
    colour — in particular ``QA|MISMATCH*``, whose per-severity colours the UI
    relies on (step 8b).
    """
    recolored: set[str] = set()
    for layer in doc.layers:
        name = layer.dxf.name
        split = split_source_layer(name)
        if split is None:
            continue
        tag, bare = split
        if tag == QA_TAG:
            if name.startswith(MISMATCH_PREFIX) or bare not in QA_GEOMETRY:
                continue
        aci = PREVIEW_COLOR[tag]
        layer.dxf.color = aci
        if layer.dxf.hasattr("true_color"):
            del layer.dxf.true_color
        recolored.add(name)
    if not recolored:
        return {"layers": [], "colors": {}}
    for e in _iter_all_entities(doc):
        if e.dxf.hasattr("layer") and e.dxf.layer in recolored:
            _set_bylayer(e)
    return {"layers": sorted(recolored),
            "colors": {name: PREVIEW_COLOR[split_source_layer(name)[0]]
                       for name in sorted(recolored)}}


def _world_to_px(ax, box, png_w: int, png_h: int) -> list[list[float]]:
    """The affine world → PNG-pixel map of ``ax``, as ``[[sx,0,tx],[0,sy,ty]]``.

    ``ax.transData.transform`` gives display coordinates with the origin in
    the *bottom* left of the canvas; a PNG counts from the top, hence the
    ``png_h - y``.  ``sy`` therefore comes out **negative** — the y axis of
    the image is flipped with respect to the drawing.
    """
    (x0, y0), (x1, y1) = ax.transData.transform(((box[0], box[1]), (box[2], box[3])))
    x0, y0, x1, y1 = float(x0), float(y0), float(x1), float(y1)
    sx = (x1 - x0) / (box[2] - box[0]) if box[2] != box[0] else 0.0
    sy = (y0 - y1) / (box[3] - box[1]) if box[3] != box[1] else 0.0
    tx = x0 - sx * box[0]
    ty = (png_h - y0) - sy * box[1]
    return [[sx, 0.0, tx], [0.0, sy, ty]]


def _render_one(doc, msp, box, out_png: Path, by_handle: dict) -> dict | None:
    """Draw one picture of ``doc`` framed on ``box``.

    Returns ``{png_w, png_h, world_to_px, world_extents}`` on success and
    ``None`` when the renderer had to give up (a blank PNG is still written so
    ``/preview.png`` does not 404 — the DXF itself is unaffected).
    """
    xmin, ymin, xmax, ymax = box

    def in_view(e) -> bool:
        try:
            if e.dxftype() == "MTEXT":
                return False        # the complex MTEXT renderer needs a font
        except Exception:
            pass
        ext = None
        try:
            ext = by_handle.get(e.dxf.handle)
        except Exception:
            ext = None
        if ext is None:
            ext = _entity_extent(e)
        if ext is None:
            return True
        return not (ext[2] < xmin or ext[0] > xmax or
                    ext[3] < ymin or ext[1] > ymax)

    fig, ax = plt.subplots(figsize=(10, 6), dpi=100)
    try:
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        ax.axis("off")
        ax.set_facecolor("white")

        ctx = RenderContext(doc)
        # Force a stub FontFace so text rendering never goes looking for a
        # font on disk (the "no fonts available" failure on minimal systems).
        from ezdxf.fonts import fonts as ezdxf_fonts
        try:
            ctx.fonts = {"": ezdxf_fonts.FontFace()}
        except Exception:
            pass

        out_b = MatplotlibBackend(ax, adjust_figure=False)
        config = draw_config.Configuration(color_policy=draw_config.ColorPolicy.COLOR)
        Frontend(ctx, out_b, config=config).draw_layout(
            msp, finalize=True, filter_func=in_view)
        # ezdxf's backend switches autoscale back on, forces an "equal" datalim
        # aspect and would resize the figure; put the requested frame back and
        # let the aspect adjust the axes *box*, so the drawing keeps exactly
        # the extents that were measured (and said so in the report).
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        # The transform is only final once the canvas has been drawn.
        fig.canvas.draw()
        png_w, png_h = (int(v) for v in fig.canvas.get_width_height())
        meta = {
            "png_w": png_w,
            "png_h": png_h,
            "world_to_px": _world_to_px(ax, box, png_w, png_h),
            "world_extents": [float(v) for v in box],
        }
        fig.savefig(str(out_png), bbox_inches=None, pad_inches=0, facecolor="white")
        return meta
    except Exception as e:                       # noqa: BLE001 — reported below
        _write_blank_png(out_png)
        print(f"  WARN: preview render failed (non-fatal): {e}")
        return None
    finally:
        plt.close(fig)


def _write_blank_png(out_png: Path) -> None:
    try:
        from PIL import Image
        Image.new("RGB", (10, 10), "white").save(str(out_png))
    except Exception:
        pass


def _render_preview(dxf_path: Path, out_png: Path, *, grid_png: Path | None = None,
                    meta_path: Path | None = None, doc=None) -> dict:
    """Render the overlay previews and report what was measured.

    Two pictures:

    * ``out_png``   the **full** extents — a stray entity at (1e7, 1e7) is in
      this picture even though it makes the drawing a dot.  It is also in
      ``report.outliers``; hiding it is what this replaced.
    * ``grid_png``  the drawing without the QA grid, +10% — the readable one.

    The colours are applied to the in-memory document only (``superimposed.dxf``
    keeps the colours stage 7 wrote).  ``preview_meta.json`` carries the
    world → pixel map of the main picture so the UI can click a finding and
    zoom to it.
    """
    _ensure_matplotlib_has_fonts()
    _patch_font_cache()

    if doc is None:
        doc = dxfio.read_internal(str(dxf_path))

    # The renderer raises on the first dangling INSERT; drop them and count.
    dangling = _drop_dangling_inserts(doc)
    msp = doc.modelspace()

    ext, by_handle = _collect_extents(doc)
    recolor = _recolor_for_preview(doc)

    summary: dict = {
        "extents": ext.to_dict(),
        "outliers": ext.outliers,
        "outliers_total": ext.outlier_total,
        "dangling_inserts_dropped": len(dangling),
        "dangling_inserts": dangling[:MAX_DANGLING],
        "entity_boxes": ext.entity_boxes,
        "colors": recolor["colors"],
        "images": [],
    }
    if dangling:
        print(f"  WARN: dropped {len(dangling)} INSERT(s) with a missing block "
              "definition (see report.json → dangling_inserts)")
    if ext.outlier_total:
        print(f"  WARN: {ext.outlier_total} entit(ies) far outside the MECH grid box "
              f"(reported, not hidden) — worst: "
              + ", ".join(f"{o['layer'] or o['type']}@{o['handle']}" for o in ext.outliers[:3]))

    targets = [(out_png, ext.full, PREVIEW_PNG)]
    if grid_png is not None:
        grid_box = _pad(ext.grid, GRID_PAD)
        targets.append((grid_png, grid_box, PREVIEW_GRID_PNG))
    fallback = None
    for target, box, name in targets:
        if box is None:
            continue
        meta = _render_one(doc, msp, box, target, by_handle)
        entry = {"name": name, "world_extents": [float(v) for v in box]}
        if meta:
            entry.update({k: meta[k] for k in ("png_w", "png_h", "world_to_px")})
            if name == PREVIEW_PNG:
                fallback = meta
        else:
            entry["rendered"] = False
        summary["images"].append(entry)

    if meta_path is not None and fallback is not None:
        meta_path.write_text(json.dumps(fallback, indent=2))
        summary["meta"] = fallback
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# counts
# ─────────────────────────────────────────────────────────────────────────────

def _count_primitives(doc, *, by_source: bool) -> tuple[dict, dict, dict]:
    """Count primitives of ``doc`` — modelspace plus everything under INSERTs.

    Returns ``(totals, by_type, by_layer)``.  A primitive inherits the source
    of the *modelspace* entity it descends from, not its own layer: almost all
    STR geometry sits inside block definitions on layer ``0``, which carries
    no source prefix at all.  With ``by_source=False`` (the "in" counts, taken
    on a cleaned drawing that has no prefixes yet) everything lands under
    ``"ALL"``.
    """
    totals: dict[str, int] = {}
    by_type: dict[str, dict[str, int]] = {}
    by_layer: dict[str, dict[str, int]] = {}
    for e in doc.modelspace():
        if by_source:
            key = _source_of(e) or "OTHER"
            layer = e.dxf.layer if e.dxf.hasattr("layer") else ""
        else:
            key, layer = "ALL", "*"
        try:
            primitives = list(disassemble.recursive_decompose([e]))
        except Exception:
            primitives = [e]
        totals[key] = totals.get(key, 0) + len(primitives)
        types = by_type.setdefault(key, {})
        layers = by_layer.setdefault(key, {})
        for p in primitives:
            t = p.dxftype()
            types[t] = types.get(t, 0) + 1
            layers[layer] = layers.get(layer, 0) + 1
    return totals, by_type, by_layer


def _counts(job_id: str, superimposed, *, pair_dir=None) -> dict:
    """``mech_in`` / ``str_in`` (cleaned pair) vs ``mech_out`` / ``str_out``.

    "in" counts the cleaned MECH/STR files the overlay consumed, "out" the
    primitives of the superimposed file grouped by layer prefix.  They agree
    unless the overlay dropped or exploded something, which is also recorded
    in ``report.overlay.failures``.
    """
    mech_in = str_in = None
    try:
        mech_path, str_path = roles_mod.resolve_pair(job_id, pair_dir)
    except Exception:
        mech_path = str_path = None
    if mech_path is not None and Path(mech_path).exists():
        m_totals, _, _ = _count_primitives(
            dxfio.read_internal(str(mech_path)), by_source=False)
        mech_in = m_totals.get("ALL", 0)
    if str_path is not None and Path(str_path).exists():
        s_totals, _, _ = _count_primitives(
            dxfio.read_internal(str(str_path)), by_source=False)
        str_in = s_totals.get("ALL", 0)

    totals, by_type, by_layer = _count_primitives(superimposed, by_source=True)
    return {
        "mech_in": mech_in,
        "str_in": str_in,
        "mech_out": totals.get(MECH_TAG, 0),
        "str_out": totals.get(STR_TAG, 0),
        "qa_out": totals.get(QA_TAG, 0),
        "other_out": totals.get("OTHER", 0),
        "by_layer_source": by_layer,
        "by_type_source": by_type,
    }


# ─────────────────────────────────────────────────────────────────────────────
# report
# ─────────────────────────────────────────────────────────────────────────────

def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _manifest_section(job_id: str) -> list[dict]:
    """The roles manifest as plain dicts (never a filename guess)."""
    try:
        entries = roles_mod.load_manifest(job_id)
        if entries is None:
            entries = roles_mod.resolve_inputs(job_id)
    except Exception:
        return []
    return [asdict(e) for e in entries]


def _fit_section(transform: dict | None) -> dict:
    """Fit quality as the UI wants it, read from ``transform.json``."""
    if not transform:
        return {}
    quality = transform.get("quality") or {}
    return {
        "model": transform.get("model"),
        "status": quality.get("status"),
        "quality": quality,
        "scale": transform.get("scale"),
        "rotation_deg": transform.get("rotation_deg"),
        "mirrored": transform.get("mirrored"),
        "rms_residual": transform.get("rms_residual"),
        "max_residual": transform.get("max_residual"),
        "loo_max": transform.get("loo_max"),
        "n_inliers": transform.get("n_inliers"),
        "n_total": transform.get("n_total"),
        "median_spacing": transform.get("median_spacing"),
        "outliers": list(transform.get("outliers") or []),
        "units": transform.get("units"),
        "warnings": list(quality.get("warnings") or []),
    }


def _overlay_section(payload: dict | None) -> dict:
    """What stage 7 did, without the multi-thousand-entry handle lists."""
    if not payload:
        return {}
    keep = ("failures", "renamed_resources", "dropped", "exploded",
            "import_method", "mech_entities", "str_entities_imported",
            "str_entities_total", "by_type_str", "warnings", "qa_grid",
            "style", "source_layers")
    return {k: payload[k] for k in keep if k in payload}


def _build_report(job_id: str, dxf_path: Path, *, root: Path | None = None,
                  preview: dict | None = None, counts: dict | None = None,
                  pair_dir=None) -> dict:
    """Aggregate everything we know about this job into one JSON file.

    When `root` is set (Slice-1 multi-pair runs), all artifact paths
    are resolved under `root` instead of `jobs/<id>/`. The legacy
    default (`root=None`) keeps the same path shape as before.
    """
    d = root if root is not None else job_dir(job_id)
    report = {
        "job_id": job_id,
        "input_dir": str(job_dir(job_id) / "uploads"),
        "cleaned_dir": str(job_dir(job_id) / "cleaned"),
        "output_dxf": str(dxf_path),
        "output_size_bytes": dxf_path.stat().st_size if dxf_path.exists() else 0,
    }

    # Validate + inspect
    for key, rel in (("validate", ("stages", "validate.json")),
                     ("inspect", ("stages", "inspect.json")),
                     ("clean_stats", ("stages", "clean_stats.json")),
                     ("axes_suggestions", ("stages", "axes_suggestions.json")),
                     ("convention", ("axes", "convention.json")),
                     ("audit", ("stages", "audit.json"))):
        data = _load_json(d.joinpath(*rel))
        if data is not None:
            report[key] = data

    t_path = d / "transform.json"
    transform = _load_json(t_path)
    if transform is not None:
        aff = Affine2D.from_json(transform)     # full 2-D affine (rotation-aware)
        report["transform"] = {
            # legacy axis-aligned keys (kept for the UI / old reports)
            "scale_x": transform["scale_x"], "scale_y": transform["scale_y"],
            "tx": transform["tx"], "ty": transform["ty"],
            # the actual model
            "model": transform.get("model"),
            "matrix": transform.get("matrix", aff.to_matrix()),
            "scale": transform.get("scale"),
            "rotation_deg": transform.get("rotation_deg"),
            "mirrored": transform.get("mirrored"),
            # quality
            "rms_residual": transform["rms_residual"],
            "max_residual": transform["max_residual"],
            "loo_max": transform.get("loo_max"),
            "n_inliers": transform.get("n_inliers"),
            "n_total": transform.get("n_total"),
            "outliers": transform.get("outliers", []),
            "units": transform.get("units"),
            "quality": transform.get("quality"),
            "x_labels": transform["x_labels"],
            "y_labels": transform["y_labels"],
        }
        report["fit"] = _fit_section(transform)

    overlay_report = _load_json(d / "stages" / overlay.REPORT_NAME)
    if overlay_report is not None:
        report["overlay"] = _overlay_section(overlay_report)

    report["roles"] = _manifest_section(job_id)
    report["counts"] = counts if counts is not None else _empty_counts()

    preview = preview or {}
    report["extents"] = preview.get("extents", {})
    report["outliers"] = preview.get("outliers", [])
    report["outliers_total"] = preview.get("outliers_total", 0)
    report["dangling_inserts_dropped"] = preview.get("dangling_inserts_dropped", 0)
    report["dangling_inserts"] = preview.get("dangling_inserts", [])
    report["preview"] = {
        "images": [
            {"name": img["name"],
             "path": f"out/{img['name']}",
             "world_extents": img.get("world_extents"),
             "png_w": img.get("png_w"), "png_h": img.get("png_h"),
             "rendered": img.get("rendered", True)}
            for img in preview.get("images", [])
        ],
        "meta": f"out/{PREVIEW_META}" if preview.get("meta") else None,
        "outlier_rule": (
            f"an entity is reported when its box lies more than "
            f"{OUTLIER_DIAGONALS:g} x the MECH grid diagonal away from the MECH "
            "box; nothing is cropped out of preview.png"),
    }

    return report


def _empty_counts() -> dict:
    return {"mech_in": None, "str_in": None, "mech_out": None, "str_out": None,
            "qa_out": None, "other_out": None,
            "by_layer_source": {}, "by_type_source": {}}


# ─────────────────────────────────────────────────────────────────────────────
# entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(job_id: str, *, pair_dir=None) -> None:
    d = job_dir(job_id)
    root = Path(pair_dir).parent if pair_dir is not None else d
    out_dir = root / "out"
    dxf_path = out_dir / "superimposed.dxf"
    if not dxf_path.exists():
        raise RuntimeError(f"Missing {dxf_path} — stage 7 didn't produce output")

    started = time.time()

    preview_png = out_dir / PREVIEW_PNG
    grid_png = out_dir / PREVIEW_GRID_PNG
    meta_path = out_dir / PREVIEW_META

    doc = dxfio.read_internal(str(dxf_path))
    # Counts first: rendering may drop a dangling INSERT, and "out" has to
    # describe the file stage 7 actually wrote.
    counts = _counts(job_id, doc, pair_dir=pair_dir)
    summary = _render_preview(dxf_path, preview_png, grid_png=grid_png,
                              meta_path=meta_path, doc=doc)

    report_json = out_dir / REPORT_JSON
    report_json.write_text(json.dumps(
        _build_report(job_id, dxf_path, root=root, preview=summary, counts=counts,
                      pair_dir=pair_dir),
        indent=2))

    print(f"  preview: {preview_png} "
          f"({preview_png.stat().st_size // 1024 if preview_png.exists() else 0} KB)")
    if grid_png.exists():
        print(f"  preview (grid): {grid_png} "
              f"({grid_png.stat().st_size // 1024} KB)")
    print(f"  report:  {report_json}  ({time.time() - started:.1f}s)")