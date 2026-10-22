#!/usr/bin/env python3
"""core/analyze.py — Slice 3 depth-analysis endpoint.

`/api/v1/overlay/{job_id}/analyze` exposes per-pair data the pipeline
already produces, packaged for the UI panel:

  - transform coefficients + per-intersection residuals (from
    `transform.json:intersections[]`, written by `core.fit.run`)
  - spotcheck issues per severity (from `stages/spotcheck.json`,
    written by `core.spotcheck.run`)
  - per-drawing bbox diagonal — the scale normalisation factor so the
    same `normalized_depth` value means the same thing in a mm MECH
    plan and a ft architectural drawing (the user explicitly flagged
    differing scales as a concern).

Depth metric: `normalized_depth = min(1.0, residual / (FRACTION × diag))`
where FRACTION = 0.005 — so 100% depth means the affine predicts that
grid intersection ≥ 0.5% of the MECH drawing's diagonal away from the
actual MECH geometry.

Why reuse transform residuals instead of computing a new sampler?
  - The data is already there and the smoke test's 17-issue result is
    already exercised against it.
  - A grid-sampler (Option #2 in the plan) costs O(N² × B) and needs
    extra fixture work to validate.
  - Per-saddle hotspots (Option #3) require saddle-related JSON we
    don't currently surface; can be added behind the same /analyze
    shape later without breaking callers.

Public API:
  bbox_diagonal(dxf_path) -> float
  DEPTH_FRACTION             (constant: 0.005 — i.e. 0.5% of diagonal)
  normalize_depth(residual, diag) -> float  (clamped 0..1)
  analyze_pair(job_id, pair_id=None) -> dict
"""
from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

from jobs import job_dir


# ─────────────────────────────────────────────────────────────────────────────
# Per-drawing scale
# ─────────────────────────────────────────────────────────────────────────────

# Fraction of bbox_diagonal that corresponds to depth=1.0. A residual of
# half-a-percent of the drawing's longest extent means "completely off".
DEPTH_FRACTION = 0.005

# Fallback diagonal used when bbox can't be computed (prevents divide-
# by-zero and gives a stable 1.0 when the drawing is degenerate).
FALLBACK_DIAGONAL = 1.0


def bbox_diagonal(dxf_path: Path) -> float:
    """Approximate diagonal of a DXF (in drawing units).

    Walks modelspace **and** every non-anonymous block def, collecting
    extent samples from LINE / LWPOLYLINE / INSERT entities. Drawing-
    local block-def coords are kept as-is — we're computing a scale
    reference, not measuring world-space extent, so it's fine that
    block-INSERTs are positioned at non-real coords (we sample the
    raw local extents). For TP-104 STR drawings where nearly all
    geometry lives in GridLine-* block defs, the world-coord extents
    would be much larger than the modelspace-only view, which would
    produce a depth metric calibrated against a scale the user never
    sees — so we keep this measurement local.

    Returns sqrt((xmax-xmin)^2 + (ymax-ymin)^2). Zero on empty/degenerate
    drawings.
    """
    import ezdxf
    try:
        doc = ezdxf.readfile(str(dxf_path))
    except Exception:
        return 0.0
    xs: list[float] = []
    ys: list[float] = []

    def _consume(container) -> None:
        for e in list(container):
            try:
                t = e.dxftype()
                if t == "LINE":
                    s = e.dxf.start
                    en = e.dxf.end
                    xs.append(float(s[0])); ys.append(float(s[1]))
                    xs.append(float(en[0])); ys.append(float(en[1]))
                elif t == "LWPOLYLINE":
                    try:
                        for v in e.vertices():
                            xs.append(float(v[0])); ys.append(float(v[1]))
                    except Exception:
                        pass
                elif t == "INSERT":
                    ix, iy = float(e.dxf.insert[0]), float(e.dxf.insert[1])
                    xs.append(ix); ys.append(iy)
            except Exception:
                continue

    try:
        _consume(doc.modelspace())
    except Exception:
        return 0.0
    for block in list(doc.blocks):
        try:
            name = block.name
        except Exception:
            continue
        if name.startswith("*"):
            continue  # skip paper-space / dim internal blocks
        try:
            _consume(block)
        except Exception:
            continue

    if not xs or not ys:
        return 0.0
    return math.hypot(max(xs) - min(xs), max(ys) - min(ys))


def normalize_depth(residual: float, mech_diag: float) -> float:
    """Convert a raw residual (drawing units) to 0..1.

    `DEPTH_FRACTION × mech_diag` is the "max acceptable" residual; we
    clamp residuals at 100% to keep the heatmap interpretable.
    """
    if mech_diag <= 0:
        mech_diag = FALLBACK_DIAGONAL
    return min(1.0, max(0.0, residual / (DEPTH_FRACTION * mech_diag)))


# ─────────────────────────────────────────────────────────────────────────────
# Pair roots
# ─────────────────────────────────────────────────────────────────────────────

def _pair_artifact_root(job_id: str, pair_id: str | None) -> Path:
    """Resolve the artifact root for a pair.

    - `pair_id=None` → legacy single-pair job → use the job root
      (`jobs/<id>/` directly), so `transform.json`,
      `stages/spotcheck.json`, etc., live there in the legacy layout.
    - `pair_id="MECH_vs_STR"` → Slice-1 multi-pair → use the pair root
      (`jobs/<id>/out/pairs/<pair_id>/`).
    """
    if pair_id:
        return job_dir(job_id) / "out" / "pairs" / pair_id
    return job_dir(job_id)


def _cleaned_dir(job_id: str, pair_id: str | None) -> Path:
    """Where the cleaned DXFs for this pair live."""
    return _pair_artifact_root(job_id, pair_id).parent / "cleaned" if pair_id \
        else job_dir(job_id) / "cleaned"


# ─────────────────────────────────────────────────────────────────────────────
# Top-level
# ─────────────────────────────────────────────────────────────────────────────

# Issue-to-intersection radius for hotspot aggregation. Issues within this
# many mm of an intersection's MECH coordinate count toward that
# intersection's severity tally.
HOTSPOT_RADIUS = 2000.0


def analyze_pair(job_id: str, pair_id: str | None = None) -> dict:
    """Build the analyze payload for one pair.

    Returns a JSON-serialisable dict with:
      pair_id, mech_file, str_file, scale_x, scale_y, tx, ty,
      rms_residual, max_residual, n_issues, severity_breakdown,
      intersections[{label, str, mech, predicted_mech, residual,
                      normalized_depth}],
      hotspots[{intersection, x, y, normalized_depth, issue_count,
                 severity_breakdown}],
      bbox_diagonals{mech, str}

    Raises FileNotFoundError if either transform.json or
    stages/spotcheck.json is missing — the GET handler turns that into
    a 404.
    """
    root = _pair_artifact_root(job_id, pair_id)
    t_path = root / "transform.json"
    s_path = root / "stages" / "spotcheck.json"
    if not t_path.exists():
        raise FileNotFoundError(f"transform.json missing at {t_path} — stage 6 hasn't run")
    if not s_path.exists():
        raise FileNotFoundError(f"spotcheck.json missing at {s_path} — stage 7.5 hasn't run")

    transform = json.loads(t_path.read_text())
    spotcheck = json.loads(s_path.read_text())

    # Filenames: pulled from the pair's metadata.json if Slice-1, else
    # from the cleaned/ dir's lexicographic order (legacy contract:
    # alphabetical first = MECH, second = STR).
    pair_meta_path = root / "metadata.json"
    if pair_meta_path.exists():
        meta = json.loads(pair_meta_path.read_text())
        mech_file = meta.get("mech_file")
        str_file = meta.get("str_file")
    else:
        cleaned = sorted((root / "cleaned").glob("*_clean.dxf"))
        mech_file = cleaned[0].name if cleaned else None
        str_file  = cleaned[1].name if len(cleaned) > 1 else None

    # Per-drawing bbox diagonals (in drawing units). Both feed into
    # normalized_depth — but per the user's spec, mech_diag is what
    # we normalise against (the residuals are MECH-space distances).
    cleaned_dir = root / "cleaned" if pair_id else job_dir(job_id) / "cleaned"
    mech_diag = bbox_diagonal(cleaned_dir / mech_file) if mech_file else 0.0
    str_diag  = bbox_diagonal(cleaned_dir / str_file)  if str_file  else 0.0

    # Intersections — one row per (X-label, Y-label) cross.
    intersections: list[dict] = []
    for ix in transform.get("intersections", []):
        residual = float(ix.get("residual", 0.0))
        intersections.append({
            "label": ix["label"],
            "str":   ix.get("str"),          # [x, y] in STR coords
            "mech":  ix.get("mech"),         # [x, y] in MECH coords
            "predicted_mech": ix.get("predicted_mech"),
            "residual": residual,
            "normalized_depth": round(normalize_depth(residual, mech_diag), 4),
        })

    # Issues → severity breakdown + per-intersection proximity tally.
    issues = spotcheck.get("issues", []) or []
    sev_breakdown = dict(Counter(i.get("severity", "INFO") for i in issues))

    hotspots: list[dict] = []
    for ix in intersections:
        nd = ix["normalized_depth"]
        # Only surface cells that are non-trivial in either axis (≥50%
        # normalised depth OR ≥3 nearby issues). Keeps the table
        # useful for small drawings too.
        if nd < 0.5:
            nearby = [
                i for i in issues
                if math.hypot(float(i.get("mech_x", 0.0)) - ix["mech"][0],
                              float(i.get("mech_y", 0.0)) - ix["mech"][1])
                   < HOTSPOT_RADIUS
            ]
            if len(nearby) < 3:
                continue
        else:
            nearby = [
                i for i in issues
                if math.hypot(float(i.get("mech_x", 0.0)) - ix["mech"][0],
                              float(i.get("mech_y", 0.0)) - ix["mech"][1])
                   < HOTSPOT_RADIUS
            ]
        hotspots.append({
            "intersection": ix["label"],
            "x": ix["mech"][0],
            "y": ix["mech"][1],
            "normalized_depth": nd,
            "issue_count": len(nearby),
            "severity_breakdown": dict(Counter(
                i.get("severity", "INFO") for i in nearby
            )),
        })
    # Sort hotspots: highest normalised_depth first, then most issues.
    hotspots.sort(key=lambda h: (-h["normalized_depth"], -h["issue_count"]))

    return {
        "pair_id": pair_id or "_legacy",
        "mech_file": mech_file,
        "str_file": str_file,
        "scale_x": transform.get("scale_x"),
        "scale_y": transform.get("scale_y"),
        "tx": transform.get("tx"),
        "ty": transform.get("ty"),
        "rms_residual": transform.get("rms_residual"),
        "max_residual": transform.get("max_residual"),
        "n_issues": len(issues),
        "severity_breakdown": sev_breakdown,
        "intersections": intersections,
        "hotspots": hotspots,
        "bbox_diagonals": {"mech": mech_diag, "str": str_diag},
    }
