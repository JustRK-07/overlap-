#!/usr/bin/env python3
"""core/axes.py — Stage 5.

Detects grid-axis labels in the cleaned drawings and exposes:
  - extract_labels(doc) -> list[LabelCandidate]
  - suggest(path) -> list of {prefix, axes, count, samples, ...}
  - find_axes(dxf_path, prefix, suffixes) -> list of {suffix, type, x, y}

The worker pauses here awaiting the user's grid-naming convention
selection; the frontend calls POST /axes with the chosen prefix +
axes. The worker calls resume_after_axes() once /axes/confirm is hit.

Convention detection algorithm:
  1. Walk all TEXT/MTEXT entities in modelspace + nested block defs
  2. For each text, try SUFFIX_PAT_LETTER and SUFFIX_PAT_DIGIT — keep
     whichever matches. The letter pattern captures trailing letters
     (e.g. "TP104-1A" → prefix="TP104-1", suffix="A"); the digit
     pattern captures trailing digits (e.g. "GR-A1" → prefix="GR-A",
     suffix="1"). For pure single-letter/single-digit suffixes both
     match — we keep both interpretations and let the clusterer pick.
  3. Group by prefix; rank clusters by:
        count × mixed-letter-digit bonus × gridline-proximity factor
     (see _score_cluster) and return the top suggestions.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import ezdxf

from jobs import job_dir


# Two suffix patterns. Either the trailing run of letters OR the trailing
# run of digits is the "axis suffix"; the rest is the project prefix.
# Using two patterns (instead of one union) lets us correctly split
# mixed-suffix labels like "TP104-1A" → ("TP104-1", "A") and "GR-A1"
# → ("GR-A", "1"), and gives a single unambiguous answer for any label.
SUFFIX_PAT_LETTER = re.compile(r"^(.*?)([A-Za-z]+)$")
SUFFIX_PAT_DIGIT = re.compile(r"^(.*?)(\d+)$")


# Beam-section labels — common structural section names that the
# naive prefix-suffix split would mistakenly interpret as axis
# candidates. Examples:
#   Indian:      ISMB 600, ISMC 250, ISA 5050, ISHB 400
#   British/EU:  UB 533, UC 305, PFC 200, RHS 100x60x5, SHS 80x80x5,
#                IPE 200, HEA 200, HEB 300, CHS 114.3x5
#   US:          W12x26, S6x17.25, M8x6.5, HP12x74, C6x8.2, MC6x12,
#                L4x4x1/2, HSS6x6x1/4
#   Bolt grades: M20, M16x50, 8.8
#
# These get filtered out in extract_labels() before they pollute
# axis clusters. The patterns are case-insensitive and tolerant of
# optional spaces, dashes, and "x" separators between the size
# components.
_BEAM_SECTION_PATTERNS = [
    # Indian Standard sections
    re.compile(r"^IS\s?MBE?", re.IGNORECASE),       # ISMB / ISMBE
    re.compile(r"^IS\s?MC", re.IGNORECASE),          # ISMC
    re.compile(r"^IS\s?JC", re.IGNORECASE),          # ISJC
    re.compile(r"^IS\s?LC", re.IGNORECASE),          # ISLC
    re.compile(r"^IS\s?HB", re.IGNORECASE),          # ISHB
    re.compile(r"^ISA\s", re.IGNORECASE),            # ISA 5050 (with required space)
    re.compile(r"^ISA\d", re.IGNORECASE),            # ISA5050 (no space)
    # British / European sections
    re.compile(r"^UB(?:\s|\d)", re.IGNORECASE),     # UB 533 / UB533
    re.compile(r"^UC(?:\s|\d)", re.IGNORECASE),
    re.compile(r"^PFC(?:\s|\d)", re.IGNORECASE),
    re.compile(r"^RHS(?:\s|\d|x)", re.IGNORECASE),
    re.compile(r"^SHS(?:\s|\d|x)", re.IGNORECASE),
    re.compile(r"^CHS(?:\s|\d|x)", re.IGNORECASE),
    re.compile(r"^IPE(?:\s|\d)", re.IGNORECASE),
    re.compile(r"^HEA(?:\s|\d)", re.IGNORECASE),
    re.compile(r"^HEB(?:\s|\d)", re.IGNORECASE),
    re.compile(r"^HEM(?:\s|\d)", re.IGNORECASE),
    re.compile(r"^HSS(?:\s|\d|x)", re.IGNORECASE),  # HSS 6x6x1/4 / HSS6x6x1/4
    re.compile(r"^J\s?IS", re.IGNORECASE),           # JIS (Japanese)
    # US sections (W12x26, S6x17.25, etc.) — require at least one
    # numeric component on each side of "x" to avoid matching "W12"
    # alone (which could be a beam designator).
    re.compile(r"^W\d+\s?x\s?\d", re.IGNORECASE),
    re.compile(r"^S\d+\s?x\s?\d", re.IGNORECASE),
    re.compile(r"^M\d+\s?x\s?\d", re.IGNORECASE),
    re.compile(r"^HP\d+\s?x\s?\d", re.IGNORECASE),
    re.compile(r"^C\d+\s?x\s?\d", re.IGNORECASE),
    re.compile(r"^MC\d+\s?x\s?\d", re.IGNORECASE),
    re.compile(r"^L\d+\s?x\s?\d", re.IGNORECASE),   # L4x4x1/2 (angle)
    # Bolt grades (often used as labels: "M20", "M16x50")
    re.compile(r"^M\d+(?:\s|$)", re.IGNORECASE),    # require end-of-word after digits
    # Property grade "8.8", "10.9", etc. — short numeric with a dot
    re.compile(r"^\d+\.\d+$"),
]


def _looks_like_beam_section(text: str) -> bool:
    """Return True if `text` looks like a structural-section / bolt-grade
    label rather than an axis label.

    These patterns are filtered out in `extract_labels()` before they
    pollute prefix clusters, so a cluster of "ISMB 600" / "ISMB 450"
    / "ISMC 250" never gets mistakenly scored as a grid convention.
    """
    return any(p.search(text) for p in _BEAM_SECTION_PATTERNS)


@dataclass
class LabelCandidate:
    """One TEXT/MTEXT entity interpreted as a potential grid label.

    `text` is the raw entity text (already .strip()'d). `prefix` and
    `suffix` come from SUFFIX_PAT_LETTER or SUFFIX_PAT_DIGIT. `position`
    is the entity's insertion point (x, y) — in world coords for
    modelspace labels, or in block-local coords for labels inside a
    block definition. `world_position` is the same point after
    applying the parent INSERT transform (filled in by
    `_compute_world_positions()`); this is what `_annotate_proximity`
    actually uses. `container` records whether the entity was found at
    modelspace or in a named block definition (for debugging /
    provenance).
    """

    text: str
    prefix: str
    suffix: str
    position: tuple[float, float]
    entity: object = None  # ezdxf.entity.DXFEntity — kept opaque to avoid type deps
    container: str = "modelspace"
    block_name: str | None = None  # set when container != "modelspace"
    world_position: tuple[float, float] | None = None  # filled by _compute_world_positions
    # Whether this label sits on (within threshold of) a long world-coord
    # axis-aligned LINE/LWPOLYLINE. Filled in by _annotate_proximity()
    # after the world-gridline cache is built. Used by _score_cluster()
    # to rank candidates by spatial evidence.
    on_gridline: bool = False
    distance_to_gridline: float = float("inf")


# Minimum LINE / LWPOLYLINE segment length to count as a gridline
# candidate. Matches the magic threshold used by core/fit.py for
# detecting axis-aligned gridlines. Override per-drawing if needed.
MIN_GRIDLINE_LENGTH = 100.0

# Distance threshold (drawing units) below which a TEXT is considered to
# "sit on" a gridline. Sized to be lenient enough for label text that
# floats slightly off the line (text bbox can extend 20–50 units away
# from the baseline), tight enough to reject random labels hundreds of
# units from any line. A small fudge (55 vs 50) absorbs floating-point
# noise from point-to-segment distance calculations that may land a
# pixel-perfect label at 50.000000000007 instead of exactly 50.0.
PROXIMITY_THRESHOLD = 55.0

# When the threshold is scaled by drawing size (auto mode), we use
# THRESHOLD_BBOX_FRACTION × bbox_diagonal as the threshold, but never
# below PROXIMITY_THRESHOLD. 0.05% of diagonal handles the TP-104 MECH
# case where labels sit ~600 units from their gridline in a ~1.58M-unit
# drawing while keeping STR's tight 55-unit match intact for tighter
# drawings.
THRESHOLD_BBOX_FRACTION = 0.0005


def _try_split_label(text: str) -> tuple[str, str] | None:
    """Try both suffix patterns and return whichever matches.

    For a pure-letter-suffix or pure-digit-suffix text, both patterns
    match — we keep the one whose suffix is shorter (so a label "TP1"
    yields suffix="1", not suffix="TP1" with empty prefix).
    """
    m_letter = SUFFIX_PAT_LETTER.match(text)
    m_digit = SUFFIX_PAT_DIGIT.match(text)
    candidates = []
    if m_letter:
        # Reject purely-letter labels with no prefix (e.g. raw "AB"
        # would give prefix="", suffix="AB") — these aren't axis labels.
        prefix = m_letter.group(1) or ""
        suffix = m_letter.group(2) or ""
        if prefix and len(suffix) <= 3:  # axis suffixes are 1–3 chars
            candidates.append((prefix, suffix))
    if m_digit:
        prefix = m_digit.group(1) or ""
        suffix = m_digit.group(2) or ""
        if prefix and len(suffix) <= 4:  # axis digit suffixes are 1–4 chars
            candidates.append((prefix, suffix))
    if not candidates:
        return None
    # Prefer the interpretation whose prefix is the same for the most
    # labels in the corpus. As a single-call heuristic we just pick the
    # one with the shorter suffix — single-letter/single-digit labels
    # are far more common in axis-naming conventions than 3-letter
    # abbreviations.
    return min(candidates, key=lambda c: len(c[1]))


def extract_labels(doc) -> list[LabelCandidate]:
    """Walk all TEXT/MTEXT entities (modelspace + non-anonymous blocks).

    Returns a list of `LabelCandidate` objects. Each candidate carries
    enough metadata to support gridline-proximity scoring (position,
    entity, container).

    Deduplicates by raw text (upper-cased) — the same label appearing
    twice in the drawing is recorded once.
    """
    out: list[LabelCandidate] = []
    seen: set[str] = set()

    def _record(e, container: str, block_name: str | None = None):
        if e.dxftype() not in ("TEXT", "MTEXT"):
            return
        try:
            txt = e.dxf.text.strip()
        except Exception:
            return
        if not txt:
            return
        key = txt.upper()
        if key in seen:
            return
        seen.add(key)
        # Reject beam-section / bolt-grade labels up-front so they
        # can't seed a false-positive axis cluster.
        if _looks_like_beam_section(txt):
            return
        split = _try_split_label(txt)
        if split is None:
            return
        prefix, suffix = split
        try:
            pos = (float(e.dxf.insert[0]), float(e.dxf.insert[1]))
        except Exception:
            pos = (0.0, 0.0)
        out.append(LabelCandidate(
            text=txt, prefix=prefix, suffix=suffix,
            position=pos, entity=e, container=container, block_name=block_name,
        ))

    for e in doc.modelspace():
        _record(e, "modelspace")
    for block in doc.blocks:
        if block.name.startswith("*"):
            continue
        for e in block:
            _record(e, block.name, block_name=block.name)
    return out


def _build_insert_lookup(doc) -> dict[str, tuple[float, float, float, float, float]]:
    """For every block name in the doc, pick a representative INSERT
    that references it (the first modelspace INSERT for that block)
    and return its world transform: (insert_x, insert_y, xscale,
    yscale, rotation_rad).

    Note: ezdxf stores INSERT rotation in **degrees** (AutoCAD
    convention). We convert to radians here so `_transform_pt` can
    pass the value directly to `math.cos` / `math.sin`.

    Used to map block-local line endpoints and label positions into
    world coords. If a block is referenced by multiple INSERTs at
    different positions, the first one wins — TP-104 STR/STR-like
    drawings use a single anchor INSERT per block.

    Falls back to (0, 0, 1, 1, 0) for blocks with no INSERT reference
    (so block-local coords are treated as already world-coords).
    """
    lookup: dict[str, tuple[float, float, float, float, float]] = {}
    for ins in doc.modelspace():
        if ins.dxftype() != "INSERT":
            continue
        try:
            name = ins.dxf.name
        except Exception:
            continue
        if name in lookup:
            continue
        try:
            ix = float(ins.dxf.insert[0])
            iy = float(ins.dxf.insert[1])
            sx = float(ins.dxf.xscale)
            sy = float(ins.dxf.yscale)
            # ezdxf stores rotation in DEGREES. Convert to radians.
            r_deg = float(getattr(ins.dxf, "rotation", 0) or 0)
            r_rad = math.radians(r_deg)
        except Exception:
            continue
        lookup[name] = (ix, iy, sx, sy, r_rad)
    return lookup


def _transform_pt(local: tuple[float, float],
                  tx: tuple[float, float, float, float, float]) -> tuple[float, float]:
    """Apply INSERT transform (insert_x, insert_y, xscale, yscale,
    rotation_rad) to a local point.

    Order of operations (matches AutoCAD):
      1. Scale by (xscale, yscale) — non-uniform allowed.
      2. Rotate by rotation (radians, counter-clockwise) around the
         block's local origin.
      3. Translate by (insert_x, insert_y).
    """
    ix, iy, sx, sy, r = tx
    lx, ly = local
    # 1. Scale
    sx_pt = sx * lx
    sy_pt = sy * ly
    # 2. Rotate (if rotation is non-zero)
    if abs(r) > 1e-12:
        cos_r = math.cos(r)
        sin_r = math.sin(r)
        rx = cos_r * sx_pt - sin_r * sy_pt
        ry = sin_r * sx_pt + cos_r * sy_pt
    else:
        rx, ry = sx_pt, sy_pt
    # 3. Translate
    return (ix + rx, iy + ry)


def _build_world_gridline_cache(doc, *, min_length: float = MIN_GRIDLINE_LENGTH) -> list:
    """Build a cache of all long axis-aligned gridline segments in
    world coordinates, as `((x1, y1), (x2, y2))` tuples.

    Modelspace lines: kept as-is (already world coords).
    Block-def lines: transformed by the parent INSERT (insert + scale)
    so they land in world coords. This is the fix for the prior
    mixed-coord-system bug — comparing block-local label positions
    against block-local line endpoints gave false-positive proximity.
    """
    from .geom import long_axis_lines
    insert_lookup = _build_insert_lookup(doc)

    segs = []
    # 1) Modelspace lines are already in world coords.
    segs.extend(long_axis_lines(doc.modelspace(), min_length=min_length))

    # 2) Block-def lines: transform by the block's parent INSERT.
    for block in doc.blocks:
        if block.name.startswith("*"):
            continue
        tx = insert_lookup.get(block.name)
        if tx is None:
            # No INSERT references this block — lines would be invisible
            # in the rendered drawing, so skip them.
            continue
        for (p, q) in long_axis_lines(block, min_length=min_length):
            segs.append((_transform_pt(p, tx), _transform_pt(q, tx)))

    return segs


def _compute_world_positions(doc, labels: list[LabelCandidate]) -> None:
    """Mutate `labels` in-place: set `world_position` for each.

    Modelspace labels: `world_position = position` (already world coords).
    Block-def labels: apply the parent INSERT transform.
    """
    insert_lookup = _build_insert_lookup(doc)
    for c in labels:
        if c.container == "modelspace" or c.block_name is None:
            c.world_position = c.position
            continue
        tx = insert_lookup.get(c.block_name)
        if tx is None:
            c.world_position = c.position  # fallback: treat as world
            continue
        c.world_position = _transform_pt(c.position, tx)


def _annotate_proximity(labels: list, world_gridlines: list,
                        threshold: float = PROXIMITY_THRESHOLD) -> None:
    """Mutate `labels` in-place: set `on_gridline` and
    `distance_to_gridline` based on the closest world-coord gridline.

    A label is considered "on a gridline" if its world position sits
    within `threshold` drawing units of any long axis-aligned world-
    coord segment. Requires `world_position` to be set on each label —
    call `_compute_world_positions()` first.
    """
    if not world_gridlines:
        return
    for c in labels:
        pos = c.world_position
        if pos is None:
            continue
        px, py = pos
        best = float("inf")
        for (x1, y1), (x2, y2) in world_gridlines:
            from .geom import point_segment_distance
            d = point_segment_distance(px, py, x1, y1, x2, y2)
            if d < best:
                best = d
                if best <= threshold:
                    break  # can't get closer than threshold; early exit
        c.distance_to_gridline = best
        c.on_gridline = best <= threshold


def _adaptive_threshold(doc, *, base: float = PROXIMITY_THRESHOLD,
                        fraction: float = THRESHOLD_BBOX_FRACTION) -> float:
    """Compute a per-drawing proximity threshold.

    Uses `max(base, fraction × bbox_diagonal)` so:
      - Tight drawings (small bbox) get the fixed `base` threshold.
      - Loose drawings (large bbox, e.g. mm-scale MECH plans where
        axis labels sit ~600 units from their gridline in a ~1.58M-unit
        drawing) get the scaled threshold.
    Falls back to `base` if bbox can't be computed.
    """
    try:
        extmin = extmax = None
        for e in doc.modelspace():
            try:
                if e.dxftype() == "LINE":
                    p1, p2 = e.dxf.start, e.dxf.end
                    x1, y1 = float(p1[0]), float(p1[1])
                    x2, y2 = float(p2[0]), float(p2[1])
                elif e.dxftype() == "INSERT":
                    x1, y1 = float(e.dxf.insert[0]), float(e.dxf.insert[1])
                    x2, y2 = x1, y1
                else:
                    continue
                if extmin is None:
                    extmin = (min(x1, x2), min(y1, y2))
                    extmax = (max(x1, x2), max(y1, y2))
                else:
                    extmin = (min(extmin[0], x1, x2), min(extmin[1], y1, y2))
                    extmax = (max(extmax[0], x1, x2), max(extmax[1], y1, y2))
            except Exception:
                continue
        if extmin is None:
            return base
        diag = math.hypot(extmax[0] - extmin[0], extmax[1] - extmin[1])
        return max(base, fraction * diag)
    except Exception:
        return base


def _score_cluster(cluster: list) -> float:
    """Score a cluster of LabelCandidates sharing the same prefix.

    Higher score = more likely to be the grid-axis convention.

        score = count × mixed-letter-digit-bonus × proximity-factor

    where:
      - count: raw number of labels in the cluster.
      - mixed-letter-digit-bonus: 2.0 if the cluster contains both
        letter-suffix and digit-suffix labels (typical X/Y axis mix),
        else 1.0. A real axis convention almost always has both.
      - proximity-factor: 1.0 + 2.0 × (n_on_gridline / count). Clusters
        where most labels sit near long lines get up to 3× boost.
        Pure-text clusters (e.g. beam labels that happen to share a
        prefix) get no boost — they don't sit on gridlines.
    """
    count = len(cluster)
    if count == 0:
        return 0.0
    suffixes = {c.suffix for c in cluster}
    n_letters = sum(1 for s in suffixes if s.isalpha())
    n_digits = sum(1 for s in suffixes if s.isdigit())
    mixed = 2.0 if (n_letters and n_digits) else 1.0
    n_on = sum(1 for c in cluster if c.on_gridline)
    prox = 1.0 + 2.0 * (n_on / count)
    return float(count) * mixed * prox


def suggest(path: Path) -> list[dict]:
    """Suggest grid conventions found in a cleaned DXF.

    Uses the gridline-proximity score from `_score_cluster` to rank
    candidate prefixes. Returns up to 4 conventions, sorted by score
    descending. The output dict carries `score` and
    `n_on_gridline` so callers can see why a particular convention
    was preferred.

    Gridline-proximity comparison is now done in WORLD coordinates —
    modelspace line endpoints are kept as-is, block-def line endpoints
    are transformed by their parent INSERT (insert + scale × local),
    and block-def label positions are similarly transformed. This
    avoids the prior bug where comparing block-local label positions
    against block-local line endpoints gave false-positive proximity.
    """
    doc = ezdxf.readfile(str(path))
    labels = extract_labels(doc)
    _compute_world_positions(doc, labels)
    world_gridlines = _build_world_gridline_cache(doc)
    threshold = _adaptive_threshold(doc)
    _annotate_proximity(labels, world_gridlines, threshold=threshold)

    by_prefix: dict[str, list[LabelCandidate]] = defaultdict(list)
    for c in labels:
        by_prefix[c.prefix].append(c)

    out = []
    for prefix, items in by_prefix.items():
        # Soft floor: need >= 2 unique suffixes to count as a real
        # convention. (The old code used a hard [4, 20] floor which
        # rejected small projects.)
        suffixes = sorted({c.suffix for c in items})
        if len(suffixes) < 2:
            continue
        score = _score_cluster(items)
        n_on = sum(1 for c in items if c.on_gridline)
        samples = [c.text for c in items[:5]]
        out.append({
            "prefix": prefix,
            "axes": suffixes,
            "count": len(items),
            "n_on_gridline": n_on,
            "score": round(score, 3),
            "samples": samples,
        })

    out.sort(key=lambda x: -x["score"])
    return out[:4]


def find_axes(dxf_path: Path, prefix: str, suffixes: list[str]) -> list[dict]:
    """Strict match: TEXT equals prefix + suffix (case-insensitive).

    Returns one entry per suffix with its (x, y) position in the drawing.
    """
    wanted = {(prefix + sfx).strip().upper(): sfx for sfx in suffixes}
    seen_at: dict[str, tuple[float, float]] = {}

    def _record(e):
        if e.dxftype() not in ("TEXT", "MTEXT"):
            return
        try:
            txt = e.dxf.text.strip().upper()
        except Exception:
            return
        if txt not in wanted or txt in seen_at:
            return
        try:
            pos = (float(e.dxf.insert[0]), float(e.dxf.insert[1]))
        except Exception:
            pos = (0.0, 0.0)
        seen_at[txt] = pos

    doc = ezdxf.readfile(str(dxf_path))
    for e in doc.modelspace():
        _record(e)
    for block in doc.blocks:
        if block.name.startswith("*"):
            continue
        for e in block:
            _record(e)

    found = []
    for sfx in suffixes:
        key = (prefix + sfx).strip().upper()
        if key in seen_at:
            x, y = seen_at[key]
            found.append({
                "suffix": sfx,
                "type": "letter" if sfx.isalpha() else "number",
                "x": x,
                "y": y,
            })
    return found


def extract_labels_for_path(dxf_path: Path) -> list["LabelCandidate"]:
    """Convenience wrapper: open a path, walk TEXT/MTEXT, return labels."""
    doc = ezdxf.readfile(str(dxf_path))
    return extract_labels(doc)


def run(job_id: str) -> None:
    """Stage 5 entrypoint — runs auto-suggest and writes to disk."""
    d = job_dir(job_id)
    cleaned = d / "cleaned"
    stages = d / "stages"
    stages.mkdir(exist_ok=True)

    cleaned_dxfs = sorted(cleaned.glob("*_clean.dxf"))
    if len(cleaned_dxfs) != 2:
        cleaned_dxfs = sorted(cleaned.glob("*.dxf"))[:2]

    sugs_a = suggest(cleaned_dxfs[0]) if len(cleaned_dxfs) > 0 else []
    sugs_b = suggest(cleaned_dxfs[1]) if len(cleaned_dxfs) > 1 else []

    common = []
    for sa in sugs_a:
        for sb in sugs_b:
            if sa["prefix"] == sb["prefix"]:
                common.append({**sa, "count": sa["count"] + sb["count"]})
                break

    if common:
        final = sorted(common, key=lambda x: -x["count"])[:4]
    else:
        final = sorted(sugs_a[:1] + sugs_b[:1], key=lambda x: -x["count"])[:4]

    (stages / "axes_suggestions.json").write_text(json.dumps({
        "drawing_a": cleaned_dxfs[0].name if len(cleaned_dxfs) > 0 else None,
        "drawing_b": cleaned_dxfs[1].name if len(cleaned_dxfs) > 1 else None,
        "suggestions": final,
    }, indent=2))
