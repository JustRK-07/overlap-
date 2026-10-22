#!/usr/bin/env python3
"""core/package.py — Stage 8.

Renders a small preview PNG of the overlay and writes a JSON report
describing the entire pipeline run. The final DXF (already produced by
stage 7) is left in place.

Reads:  jobs/<id>/out/superimposed.dxf
Writes: jobs/<id>/out/preview.png
        jobs/<id>/out/report.json
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import ezdxf
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

from jobs import job_dir


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
        "/usr/share/fonts/noto/DejaVuSans.ttf",
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


def _entity_extent(e) -> tuple[float, float, float, float] | None:
    """Return (xmin, ymin, xmax, ymax) for an entity, or None.

    `e.bbox()` raises AttributeError on raw DXF primitives (Line,
    Lwpolyline) which don't override it — fall back to extracting
    coords directly. Also works for INSERTs and TEXT.
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


def _compute_extents(doc) -> tuple[float, float, float, float] | None:
    """Return (xmin, ymin, xmax, ymax) of modelspace + block contents,
    padded by 5%.

    Walks modelspace entities AND expands each INSERT into its block
    contents (transformed by the INSERT's position + scale) so the
    extents cover geometry nested inside block defs — which the
    overlay stage often leaves at the INSERTs' origin (after pre-
    scaling). `doc.modelspace().bbox()` only sees the INSERT's own
    origin, missing the geometry nested inside the block.

    When the geometry spans a huge range (typical after affine
    transforms that left some coordinates unrestrained), use a
    percentile-based crop so the renderer focuses on the bulk of
    the content rather than stretching the view to fit outliers.
    """
    xmin = ymin = float("inf")
    xmax = ymax = float("-inf")
    msp = doc.modelspace()
    name_to_block = {b.name: b for b in doc.blocks}

    direct_samples_x: list[float] = []
    direct_samples_y: list[float] = []
    insert_samples_x: list[float] = []
    insert_samples_y: list[float] = []

    def _consider_direct(x0, y0, x1, y1):
        nonlocal xmin, ymin, xmax, ymax
        xmin = min(xmin, x0, x1)
        ymin = min(ymin, y0, y1)
        xmax = max(xmax, x0, x1)
        ymax = max(ymax, y0, y1)
        direct_samples_x.extend([x0, x1])
        direct_samples_y.extend([y0, y1])

    def _consider_insert(x0, y0, x1, y1):
        insert_samples_x.extend([x0, x1])
        insert_samples_y.extend([y0, y1])

    # Direct modelspace geometry
    for e in msp:
        ext = _entity_extent(e)
        if ext is not None:
            _consider_direct(*ext)

    if xmin == float("inf"):
        return None

    # Compute the canonical drawing range from direct modelspace
    # extents using an IQR-based bounding window. This filters out:
    #   - stray entities that landed outside the bulk drawing
    #     (e.g., a single `RTR` title-block INSERT at (307K, 1.4M))
    #   - a few out-of-bounds outliers from `_strategy_A`'s pre-baking
    # We use the IQR (Q1-Q3) of direct samples as the "drawing
    # range" — robust to a single outlier like the RTR INSERT — so
    # the renderer focuses on the actual structural content.
    direct_samples_x.sort()
    direct_samples_y.sort()
    n = len(direct_samples_x)
    if n >= 100:
        q1x, q3x = direct_samples_x[int(n * 0.25)], direct_samples_x[int(n * 0.75)]
        q1y, q3y = direct_samples_y[int(n * 0.25)], direct_samples_y[int(n * 0.75)]
    else:
        q1x, q3x = xmin, xmax
        q1y, q3y = ymin, ymax
    direct_cx = (q1x + q3x) / 2
    direct_cy = (q1y + q3y) / 2
    # Bounding window: 5× IQR centered on the median. Tighter than
    # 5× the full 98%-2% range (which can be 1e6 for drawings that
    # have a single stray INSERT), but big enough to include real
    # geometry just outside the bulk.
    direct_w = max(q3x - q1x, 1.0)
    direct_h = max(q3y - q1y, 1.0)
    bound_w = max(direct_w * 5.0, 1e4)
    bound_h = max(direct_h * 5.0, 1e4)
    x_lo = direct_cx - bound_w
    x_hi = direct_cx + bound_w
    y_lo = direct_cy - bound_h
    y_hi = direct_cy + bound_h

    # Expand INSERTs: visit each block's contents, transformed by the
    # INSERT's insertion + scale. Drop samples that fall far outside
    # the canonical drawing range (typically because `_strategy_A`
    # baked geometry into a block but its local coords sit at
    # unrealistic values that exceed the canonical drawing scale).
    for ins in msp:
        try:
            if ins.dxftype() != "INSERT":
                continue
            block = name_to_block.get(ins.dxf.name)
            if block is None:
                continue
            ix = float(ins.dxf.insert[0])
            iy = float(ins.dxf.insert[1])
            sx = float(ins.dxf.xscale or 1.0)
            sy = float(ins.dxf.yscale or sx)
            for be in block:
                ext = _entity_extent(be)
                if ext is None:
                    continue
                wx0 = ix + sx * ext[0]
                wy0 = iy + sy * ext[1]
                wx1 = ix + sx * ext[2]
                wy1 = iy + sy * ext[3]
                # Skip samples wildly outside the bounding window.
                if (wx0 < x_lo and wx1 < x_lo) or (wx0 > x_hi and wx1 > x_hi):
                    continue
                if (wy0 < y_lo and wy1 < y_lo) or (wy0 > y_hi and wy1 > y_hi):
                    continue
                _consider_insert(wx0, wy0, wx1, wy1)
        except Exception:
            continue

    # Reset the final extents using a percentile of COMBINED samples
    # (direct + accepted insert-expanded). When the distribution is
    # heavily bimodal — most geometry clustered near `[210K, -160K]`
    # but a handful of out-of-range entities like the `RTR` title-
    # block INSERT land at `(307K, 1.4M)` — use a tighter crop +
    # IQR-style outlier filter so the canvas focuses on the bulk.
    all_x = direct_samples_x + insert_samples_x
    all_y = direct_samples_y + insert_samples_y
    if len(all_x) < 100:
        if all_x:
            xmin, xmax = min(all_x), max(all_x)
            ymin, ymax = min(all_y), max(all_y)
    else:
        all_x.sort()
        all_y.sort()
        n = len(all_x)

        # 1st pass: 5%-95th percentile
        xmin = all_x[int(n * 0.05)]
        xmax = all_x[int(n * 0.95)]
        ymin = all_y[int(n * 0.05)]
        ymax = all_y[int(n * 0.95)]

        # 2nd pass: IQR-based tightening. A handful of outliers
        # (`RTR` at 1.4M y, etc.) inflate the 95th percentile
        # without contributing real geometry. Tighten to median ±
        # 1.5×IQR if the spread is wildly larger than the IQR.
        q1x, q3x = all_x[int(n * 0.25)], all_x[int(n * 0.75)]
        q1y, q3y = all_y[int(n * 0.25)], all_y[int(n * 0.75)]
        med_x = all_x[int(n * 0.50)]
        med_y = all_y[int(n * 0.50)]
        iqr_x = q3x - q1x
        iqr_y = q3y - q1y

        # Try IQR-based crop
        candidate_xmin = med_x - 1.0 * iqr_x - 0.5 * max(iqr_x, 1.0)
        candidate_xmax = med_x + 1.0 * iqr_x + 0.5 * max(iqr_x, 1.0)
        candidate_ymin = med_y - 1.0 * iqr_y - 0.5 * max(iqr_y, 1.0)
        candidate_ymax = med_y + 1.0 * iqr_y + 0.5 * max(iqr_y, 1.0)

        # The IQR crop gives a tight box around the bulk. If the
        # 5th-95th range is wider than 3× the IQR, prefer the IQR
        # crop — it's more representative of the actual drawing.
        cur_w = xmax - xmin
        cur_h = ymax - ymin
        if cur_w > 3 * max(iqr_x, 1.0):
            xmin = candidate_xmin
            xmax = candidate_xmax
        if cur_h > 3 * max(iqr_y, 1.0):
            ymin = candidate_ymin
            ymax = candidate_ymax

        # Ensure a sensible minimum span (so we don't crop to a
        # degenerate region around the median if the IQR is tiny).
        if xmax - xmin < 1e4:
            cx = (xmin + xmax) / 2
            xmin, xmax = cx - 5e3, cx + 5e3
        if ymax - ymin < 1e4:
            cy = (ymin + ymax) / 2
            ymin, ymax = cy - 5e3, cy + 5e3

    # Pad the cropped extents by 5% on each side.
    pad_x = max((xmax - xmin) * 0.05, 1.0)
    pad_y = max((ymax - ymin) * 0.05, 1.0)
    return (xmin - pad_x, ymin - pad_y, xmax + pad_x, ymax + pad_y)


def _drop_dangling_inserts(doc) -> int:
    """Remove INSERT entities that reference non-existent block defs.

    The overlay stage's `Importer` copies STR blocks (renamed with a
    `_STR_` prefix) into the MECH document. Sometimes the Importer
    misses a block — typically an empty geometry-only block like
    `GridLine-...-154899` that has no entities and gets pruned. The
    INSERT that referenced it stays in modelspace as a dangling
    pointer, and the Frontend renderer raises on the first one,
    aborting the entire preview. We drop those INSERTs before
    rendering.

    Returns the number of INSERTs dropped.
    """
    block_names = {b.name for b in doc.blocks}
    dropped = 0
    for layout in [doc.modelspace()] + [b for b in doc.blocks if not b.name.startswith("*")]:
        for e in list(layout):
            try:
                if e.dxftype() != "INSERT":
                    continue
                if e.dxf.name not in block_names:
                    layout.delete_entity(e)
                    dropped += 1
            except Exception:
                continue
    return dropped


def _render_preview(dxf_path: Path, out_png: Path) -> None:
    """Render overlay at low res (1000x600) cropped to the drawing extents.

    Resilient to missing fonts — calls _ensure_matplotlib_has_fonts() first
    AND uses a custom FontFace so text rendering never raises. If a font
    is genuinely missing on disk, the text will be skipped (geometry-only
    preview is still produced).
    """
    _ensure_matplotlib_has_fonts()

    # Patch ezdxf's font_manager.find_best_match to short-circuit
    # when FontFace.filename is None. Without this, the
    # `complex_mtext_renderer` (used for MTEXT) and the standard text
    # renderer raise AttributeError on `'NoneType' object has no
    # attribute 'filename'`, aborting the entire preview render.
    # We wrap the cache lookup and return a FontFace with a placeholder
    # filename — the renderer will skip glyph generation but geometry
    # continues to draw.
    try:
        from ezdxf.fonts import font_manager as _fm
        from ezdxf.fonts.fonts import FontFace as _FontFace
        _CacheClass = getattr(_fm, "FontCache", None) or getattr(_fm, "FontFaceCache", None)
        if _CacheClass is None:
            raise RuntimeError("no FontCache class")
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

    # Use plain ezdxf.readfile() — `recover.readfile()` silently drops
    # ~90% of modelspace entities in superimposed DXFs that contain
    # `_STR_*` renamed blocks (it returns 249 entities instead of 2,508
    # with no errors reported). The DXF produced by the overlay stage
    # is well-formed, so plain readfile is fine; the recover fallback
    # is only for genuinely corrupt files.
    try:
        doc = ezdxf.readfile(str(dxf_path))
    except Exception:
        from ezdxf import recover
        doc, _auditor = recover.readfile(str(dxf_path))

    # Drop INSERTs whose block def is missing. The Frontend renderer
    # raises "Required block definition for ... does not exist." on
    # the first dangling reference, aborting the entire preview. This
    # can happen when the overlay stage's Importer misses a block.
    _drop_dangling_inserts(doc)

    msp = doc.modelspace()

    extents = _compute_extents(doc)
    if extents is None:
        xmin, ymin, xmax, ymax = 0.0, 0.0, 1000.0, 1000.0
    else:
        xmin, ymin, xmax, ymax = extents

    def _in_view(e):
        ext = _entity_extent(e)
        if ext is None:
            return True
        ex0, ey0, ex1, ey1 = ext
        return not (ex1 < xmin or ex0 > xmax or
                    ey1 < ymin or ey0 > ymax)

    fig, ax = plt.subplots(figsize=(10, 6), dpi=100)
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_facecolor("white")

    ctx = RenderContext(doc)

    # Force a stub FontFace so text rendering never tries to find a font
    # on disk (which is what causes "no fonts available" on minimal
    # systems). This makes TEXT entities render as outlined paths via
    # ezdxf's internal stroke fonts.
    from ezdxf.fonts import fonts as ezdxf_fonts
    try:
        ctx.fonts = {"": ezdxf_fonts.FontFace()}
    except Exception:
        pass

    # Wrap _in_view to also filter out MTEXT — when no font is registered
    # the MTEXT complex renderer raises AttributeError on
    # `font_face.filename`, aborting the whole preview. Standard TEXT
    # entities render fine; only complex MTEXT has this bug.
    def _in_view_safe(e):
        try:
            if e.dxftype() == "MTEXT":
                return False
        except Exception:
            pass
        return _in_view(e)

    out_b = MatplotlibBackend(ax)
    try:
        Frontend(ctx, out_b).draw_layout(msp, finalize=True, filter_func=_in_view_safe)
        fig.savefig(str(out_png), bbox_inches=None, pad_inches=0, facecolor="white")
    except Exception as e:
        plt.close(fig)
        # Render failed — write a minimal white PNG so /preview doesn't
        # 404. The DXF itself is unaffected.
        try:
            from PIL import Image
            Image.new("RGB", (10, 10), "white").save(str(out_png))
        except Exception:
            pass
        print(f"  WARN: preview render failed (non-fatal): {e}")
    finally:
        plt.close(fig)


def _build_report(job_id: str, dxf_path: Path, *,
                 root: Path | None = None) -> dict:
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
    val_path = d / "stages" / "validate.json"
    if val_path.exists():
        report["validate"] = json.loads(val_path.read_text())
    insp_path = d / "stages" / "inspect.json"
    if insp_path.exists():
        report["inspect"] = json.loads(insp_path.read_text())
    clean_path = d / "stages" / "clean_stats.json"
    if clean_path.exists():
        report["clean_stats"] = json.loads(clean_path.read_text())
    sug_path = d / "stages" / "axes_suggestions.json"
    if sug_path.exists():
        report["axes_suggestions"] = json.loads(sug_path.read_text())
    conv_path = d / "axes" / "convention.json"
    if conv_path.exists():
        report["convention"] = json.loads(conv_path.read_text())
    t_path = d / "transform.json"
    if t_path.exists():
        t = json.loads(t_path.read_text())
        report["transform"] = {
            "scale_x": t["scale_x"], "scale_y": t["scale_y"],
            "tx": t["tx"], "ty": t["ty"],
            "rms_residual": t["rms_residual"],
            "max_residual": t["max_residual"],
            "x_labels": t["x_labels"],
            "y_labels": t["y_labels"],
        }

    return report


def run(job_id: str, *, pair_dir=None) -> None:
    d = job_dir(job_id)
    if pair_dir is not None:
        root = Path(pair_dir).parent
    else:
        root = d
    out_dir = root / "out"
    dxf_path = out_dir / "superimposed.dxf"
    if not dxf_path.exists():
        raise RuntimeError(f"Missing {dxf_path} — stage 7 didn't produce output")

    started = time.time()

    preview_png = out_dir / "preview.png"
    _render_preview(dxf_path, preview_png)

    report_json = out_dir / "report.json"
    report_json.write_text(json.dumps(_build_report(job_id, dxf_path, root=root), indent=2))

    print(f"  preview: {preview_png} ({preview_png.stat().st_size // 1024} KB)")
    print(f"  report:  {report_json}")
