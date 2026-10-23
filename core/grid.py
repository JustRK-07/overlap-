#!/usr/bin/env python3
"""core/grid.py — World-coordinate grid detection (PLAN.md step 4).

Everything here works in WORLD coordinates obtained by composing the
ezdxf INSERT matrices (``insert.matrix44()``), so block base points,
rotation, mirroring, non-uniform scale and nesting are all exact.
There are no absolute unit thresholds and no block-name prefixes: line
length thresholds are fractions of the drawing extents, and a GridLine
block is recognised structurally (a label and a line that belong to the
same INSERT).

Public API
----------
walk_world(doc, layout=None, *, root=None, max_depth=16) -> Iterator[WorldItem]
label_occurrences(doc, prefix, suffixes) -> dict[suffix, list[LabelOcc]]
candidate_segments(doc) -> list[Segment]
merge_collinear(segs, ang_tol=1e-6, off_tol=...) -> list[GridLineCand]
detect_grid(doc, prefix, suffixes) -> GridResult
intersections(result) -> dict[(sfx_a, sfx_b), Vec2]
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterator, Sequence

from ezdxf.math import Matrix44, Vec2, Vec3

# Layers that commonly carry grid centrelines. Only a score BONUS, never a filter.
GRID_LAYERS_MECH = {
    "CEN", "CENETR LINE", "CENTER", "PHANTOM", "PANTHOM",
    "HIDDEN", "HIDDDEN", "CENTERLINE", "C-ROAD-STAN",
}

MIN_LEN_FRAC = 0.15          # merged candidate must span >= this x drawing extent
MIN_SEG_FRAC = 0.02          # raw segment pre-filter (lets dashed lines merge)
MERGE_GAP_FRAC = 0.05        # max gap between merged collinear pieces
ALONG_GAP_FRAC = 0.25        # label may sit this far (x line length) beyond an endpoint
PERP_FRAC = 0.05             # ... and this far (x line length) off the infinite line
PERP_WEIGHT = 5.0            # perpendicular distance is costlier than along-line gap
LAYER_BONUS = 0.9            # cost multiplier for GRID_LAYERS_MECH members
FAMILY_ANGLE_DEG = 20.0

_GRAPHIC_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "TEXT", "MTEXT", "ATTRIB"}


# ───────────────────────────── walk ─────────────────────────────

@dataclass
class WorldItem:
    entity: object
    m: Matrix44                      # entity coordinates -> world
    chain: tuple = ()                # block names from modelspace down
    handles: tuple = ()              # INSERT handles from modelspace down
    #: Layer a layer-"0" entity inherits here: the nearest INSERT in the
    #: chain whose layer is not "0" ("" when there is none).  Added for
    #: core/mismatch/features.py; ignored by every other consumer.
    inherited_layer: str = ""


def _insert_instances(ins):
    """Yield (suffix, insert) for an INSERT, expanding MINSERT arrays."""
    try:
        rc = int(ins.dxf.row_count)
        cc = int(ins.dxf.column_count)
    except Exception:
        rc = cc = 1
    if rc > 1 or cc > 1:
        try:
            for i, v in enumerate(ins.multi_insert()):
                yield f"#{i}", v
            return
        except Exception:
            pass
    yield "", ins


def walk_world(doc, layout=None, *, root: Matrix44 | None = None,
               max_depth: int = 16) -> Iterator[WorldItem]:
    """Depth-first walk of a layout, recursing into INSERTs.

    ``m_child = insert.matrix44() * m_parent`` (ezdxf row-vector
    convention: the left factor is applied first).  ``matrix44()``
    includes the block base point.  Every non-INSERT entity is yielded
    with its world matrix; INSERT entities themselves are yielded too
    (with the parent matrix) before their content, and an INSERT's
    ATTRIBs are yielded with the *parent* matrix (their coordinates are
    already in the INSERT's parent space).  Cycles and depth beyond
    ``max_depth`` are skipped.

    ``root`` (step 8b.3) is the matrix applied *first*: modelspace items
    are yielded with it and every INSERT composes on top, so
    ``m_child = insert.matrix44() * root * …``.  It is how the STR drawing
    is mapped into MECH world — ``Affine2D.to_matrix44()`` from
    ``transform.json``.  ``None`` means the identity, which is the
    pre-8b.3 behaviour.
    """
    if layout is None:
        layout = doc.modelspace()
    m0 = Matrix44() if root is None else root
    yield from _walk(doc, layout, m0, (), (), "", max_depth)


def _walk(doc, container, m, chain, handles, inherited, depth) -> Iterator[WorldItem]:
    for e in container:
        t = e.dxftype()
        if t != "INSERT":
            yield WorldItem(e, m, chain, handles, inherited)
            continue
        yield WorldItem(e, m, chain, handles, inherited)
        try:
            name = e.dxf.name
        except Exception:
            continue
        block = doc.blocks.get(name)
        if block is None or depth <= 0:
            continue
        if any(name.lower() == c.lower() for c in chain):     # cycle guard
            continue
        try:
            ins_layer = str(e.dxf.layer)
        except Exception:
            ins_layer = "0"
        # layer "0" keeps whatever the enclosing INSERTs established
        child_inh = ins_layer if ins_layer != "0" else inherited
        base_h = str(e.dxf.handle)
        for sfx, inst in _insert_instances(e):
            h = handles + (base_h + sfx,)
            try:
                mc = inst.matrix44() * m
            except Exception:
                continue
            for a in e.attribs:
                yield WorldItem(a, m, chain + (name,), h, child_inh)
            yield from _walk(doc, block, mc, chain + (name,), h, child_inh, depth - 1)


def _world_pt(m: Matrix44, p) -> Vec2:
    v = m.transform(Vec3(float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0))
    return Vec2(v.x, v.y)


# ───────────────────────────── labels ─────────────────────────────

@dataclass
class LabelOcc:
    world_pt: Vec2
    chain: tuple
    kind: str                         # "TEXT" | "MTEXT" | "ATTRIB"
    handles: tuple = ()
    text: str = ""


def _label_text(e) -> str | None:
    try:
        if e.dxftype() == "MTEXT":
            return e.plain_text()
        return e.dxf.text
    except Exception:
        return None


def _scan(doc):
    """One walk -> (label-ish items, segment-ish items)."""
    labels, lines = [], []
    for it in walk_world(doc):
        t = it.entity.dxftype()
        if t in ("TEXT", "MTEXT", "ATTRIB"):
            labels.append(it)
        elif t in ("LINE", "LWPOLYLINE", "POLYLINE"):
            lines.append(it)
    return labels, lines


def label_occurrences(doc, prefix: str, suffixes: Sequence[str], *, _items=None):
    """All TEXT/MTEXT/ATTRIB occurrences matching prefix+suffix (no dedup)."""
    sfx_by_up = {s.upper(): s for s in suffixes}
    alt = "|".join(re.escape(s) for s in sorted(suffixes, key=len, reverse=True))
    out: dict[str, list[LabelOcc]] = {s: [] for s in suffixes}
    if not suffixes:
        return out
    rx = re.compile(rf"^\s*{re.escape(prefix)}\s*({alt})\s*$", re.IGNORECASE)
    items = _items if _items is not None else _scan(doc)[0]
    for it in items:
        e = it.entity
        txt = _label_text(e)
        if not txt:
            continue
        mt = rx.match(txt.strip())
        if not mt:
            continue
        sfx = sfx_by_up.get(mt.group(1).upper())
        if sfx is None:
            continue
        try:
            pt = _world_pt(it.m, e.dxf.insert)
        except Exception:
            continue
        out[sfx].append(LabelOcc(pt, it.chain, e.dxftype(), it.handles, txt.strip()))
    return out


# ───────────────────────────── segments ─────────────────────────────

@dataclass
class Segment:
    a: Vec2
    b: Vec2
    layer: str
    handles: tuple
    chain: tuple

    @property
    def length(self) -> float:
        return (self.b - self.a).magnitude


def _to_wcs_pts(e, pts_xyz):
    """OCS->WCS for entities with a non-default extrusion."""
    try:
        ext = e.dxf.extrusion
    except Exception:
        return pts_xyz
    if tuple(ext) == (0.0, 0.0, 1.0):
        return pts_xyz
    ocs = e.ocs()
    return [ocs.to_wcs(p) for p in pts_xyz]


def _entity_segments(e):
    """Local-space straight (non-bulged) segments of LINE / open polylines."""
    t = e.dxftype()
    if t == "LINE":
        return [(Vec3(e.dxf.start), Vec3(e.dxf.end))]
    if t == "LWPOLYLINE":
        if e.closed:
            return []
        elev = float(e.dxf.elevation) if e.dxf.hasattr("elevation") else 0.0
        pts = e.get_points("xyb")
        xyz = _to_wcs_pts(e, [Vec3(p[0], p[1], elev) for p in pts])
        return [(xyz[i], xyz[i + 1]) for i in range(len(pts) - 1)
                if not abs(pts[i][2]) > 1e-12]
    if t == "POLYLINE":
        if not e.is_2d_polyline or e.is_closed:
            return []
        vs = list(e.vertices)
        xyz = _to_wcs_pts(e, [Vec3(v.dxf.location) for v in vs])
        return [(xyz[i], xyz[i + 1]) for i in range(len(vs) - 1)
                if not abs(vs[i].dxf.bulge) > 1e-12]
    return []


def _extent(doc, lines) -> float:
    """Robust drawing extent: max(width, height), resistant to strays.

    A single title block, stray INSERT or RTR marker sitting far outside
    the plan (the synthetic fixture puts one at 1e7) would inflate a full
    bounding box so much that no gridline can clear `MIN_LEN_FRAC * ext`
    and the fit reports zero shared intersections. We therefore size the
    drawing from the bulk of the *segment* endpoints using an
    IQR-expanded window, which ignores a handful of outliers by
    construction, and fall back to the full bbox when the sample is too
    small for that to be meaningful.
    """
    xs, ys = [], []
    for it in lines:
        for a, b in _entity_segments(it.entity):
            for p in (a, b):
                w = _world_pt(it.m, p)
                xs.append(w.x)
                ys.append(w.y)

    def _full() -> float:
        try:
            from ezdxf import bbox
            bb = bbox.extents(doc.modelspace(), fast=True)
            if bb.has_data:
                return max(bb.size.x, bb.size.y)
        except Exception:
            pass
        return max((max(xs) - min(xs)), (max(ys) - min(ys))) if xs else 0.0

    if len(xs) < 8:
        return _full()

    def _window(vals: list) -> float:
        v = sorted(vals)
        n = len(v)
        q1, q3 = v[int(n * 0.25)], v[int(n * 0.75)]
        iqr = q3 - q1
        lo = q1 - 1.5 * iqr
        hi = q3 + 1.5 * iqr
        inside = [x for x in v if lo <= x <= hi]
        # Too few survivors means the "bulk" window mis-fit the data
        # (a very small or very uniform drawing) — use the full range.
        if len(inside) < max(4, n // 20):
            return v[-1] - v[0]
        return max(inside) - min(inside)

    robust = max(_window(xs), _window(ys))
    if robust > 0.0:
        return robust
    # Degenerate bulk window (every point identical, or the sample was
    # too small) — fall back to the full bbox.
    return _full()


def candidate_segments(doc, *, _lines=None, _extent_val=None,
                       min_frac: float = MIN_SEG_FRAC) -> list[Segment]:
    """World-space straight segments long enough to be gridline pieces.

    Unit independent: the threshold is ``min_frac`` x max(extent_w, extent_h).
    (``MIN_SEG_FRAC`` is deliberately smaller than the 0.15 applied to the
    *merged* line in :func:`detect_grid`, so dashed gridlines still merge.)
    """
    lines = _lines if _lines is not None else _scan(doc)[1]
    ext = _extent_val if _extent_val is not None else _extent(doc, lines)
    thr = min_frac * ext
    out = []
    for it in lines:
        e = it.entity
        try:
            layer = e.dxf.layer
            segs = _entity_segments(e)
        except Exception:
            continue
        for a, b in segs:
            wa, wb = _world_pt(it.m, a), _world_pt(it.m, b)
            if (wb - wa).magnitude >= max(thr, 1e-12):
                out.append(Segment(wa, wb, layer, it.handles, it.chain))
    return out


@dataclass
class GridLineCand:
    point: Vec2                       # world point at the low end of the line
    dir: Vec2                         # unit direction (dx>0, or dy>0 when vertical)
    length: float
    members: list = field(default_factory=list)   # Segment
    chains: set = field(default_factory=set)
    handle_sets: set = field(default_factory=set)
    layers: set = field(default_factory=set)

    def param(self, p: Vec2) -> float:
        return self.dir.x * (p.x - self.point.x) + self.dir.y * (p.y - self.point.y)

    def perp(self, p: Vec2) -> float:
        return abs(-self.dir.y * (p.x - self.point.x) + self.dir.x * (p.y - self.point.y))


def _unit(a: Vec2, b: Vec2) -> Vec2:
    d = (b - a).normalize()
    if d.x < -1e-12 or (abs(d.x) <= 1e-12 and d.y < 0):
        d = -d
    return d


def merge_collinear(segs: Sequence[Segment], ang_tol: float = 1e-6,
                    off_tol: float = 1e-6, gap_tol: float = float("inf")
                    ) -> list[GridLineCand]:
    """Merge segments that are collinear within ``ang_tol`` rad / ``off_tol``
    units and whose along-line gap is <= ``gap_tol``."""
    clusters: list[dict] = []
    for s in segs:
        d = _unit(s.a, s.b)
        c = -d.y * s.a.x + d.x * s.a.y
        for cl in clusters:
            rd = cl["d"]
            if abs(rd.x * d.y - rd.y * d.x) <= ang_tol and abs(cl["c"] - c) <= off_tol:
                cl["segs"].append(s)
                break
        else:
            clusters.append({"d": d, "c": c, "segs": [s]})

    out: list[GridLineCand] = []
    for cl in clusters:
        segs_c = cl["segs"]
        rd = cl["d"]
        o = segs_c[0].a
        ivs = []
        for s in segs_c:
            t1 = rd.x * (s.a.x - o.x) + rd.y * (s.a.y - o.y)
            t2 = rd.x * (s.b.x - o.x) + rd.y * (s.b.y - o.y)
            ivs.append((min(t1, t2), max(t1, t2), s))
        ivs.sort(key=lambda v: v[0])
        groups, cur, cur_end = [], [ivs[0]], ivs[0][1]
        for iv in ivs[1:]:
            if iv[0] - cur_end <= gap_tol:
                cur.append(iv)
                cur_end = max(cur_end, iv[1])
            else:
                groups.append(cur)
                cur, cur_end = [iv], iv[1]
        groups.append(cur)
        for g in groups:
            longest = max((v[2] for v in g), key=lambda s: s.length)
            d = _unit(longest.a, longest.b)
            org = longest.a
            ts = []
            for _, _, s in g:
                for p in (s.a, s.b):
                    ts.append(d.x * (p.x - org.x) + d.y * (p.y - org.y))
            tmin, tmax = min(ts), max(ts)
            cand = GridLineCand(point=org + d * tmin, dir=d, length=tmax - tmin)
            for _, _, s in g:
                cand.members.append(s)
                cand.chains.add(s.chain)
                cand.handle_sets.add(s.handles)
                cand.layers.add(s.layer.upper())
            out.append(cand)
    return out


# ───────────────────────────── detection ─────────────────────────────

@dataclass
class GridAxis:
    label: str
    point: Vec2 | None = None
    dir: Vec2 | None = None
    length: float = 0.0
    n_occ: int = 0
    status: str = "missing"           # "ok" | "ambiguous" | "missing"
    reasons: list = field(default_factory=list)
    n_agree: int = 0
    occurrences: list = field(default_factory=list)


@dataclass
class GridResult:
    axes: dict                        # suffix -> GridAxis
    families: tuple                   # (list[sfx], list[sfx])
    extent: float = 0.0
    prefix: str = ""

    def ok_axes(self) -> dict:
        return {k: a for k, a in self.axes.items() if a.status == "ok"}


def _associate(occ: LabelOcc, cands: list[GridLineCand]):
    """Return (candidate index | None, reason)."""
    # 1) same-INSERT rule
    same = [i for i, c in enumerate(cands) if occ.handles and occ.handles in c.handle_sets]
    if len(same) == 1:
        return same[0], "same-insert"
    if same:
        pool, gated = same, False
    else:
        pool, gated = list(range(len(cands))), True
    best, best_cost = None, float("inf")
    for i in pool:
        c = cands[i]
        t = c.param(occ.world_pt)
        along = max(0.0, -t, t - c.length)
        perp = c.perp(occ.world_pt)
        if gated and (along > ALONG_GAP_FRAC * c.length or perp > PERP_FRAC * c.length):
            continue
        cost = PERP_WEIGHT * perp + along
        if c.layers & GRID_LAYERS_MECH:
            cost *= LAYER_BONUS
        if cost < best_cost:
            best, best_cost = i, cost
    if best is None:
        return None, "no candidate line within gates"
    return best, "nearest"


def detect_grid(doc, prefix: str, suffixes: Sequence[str]) -> GridResult:
    labels, lines = _scan(doc)
    ext = _extent(doc, lines)
    occs = label_occurrences(doc, prefix, suffixes, _items=labels)
    segs = candidate_segments(doc, _lines=lines, _extent_val=ext)
    cands = [c for c in merge_collinear(segs, 1e-6, 1e-6 * ext, MERGE_GAP_FRAC * ext)
             if c.length >= MIN_LEN_FRAC * ext]

    axes: dict[str, GridAxis] = {}
    chosen: dict[str, int] = {}
    for sfx in suffixes:
        ax = GridAxis(label=sfx, n_occ=len(occs[sfx]), occurrences=occs[sfx])
        axes[sfx] = ax
        if not occs[sfx]:
            ax.reasons.append(f"label {prefix}{sfx} not found as TEXT/MTEXT/ATTRIB")
            continue
        votes = Counter()
        for o in occs[sfx]:
            idx, why = _associate(o, cands)
            if idx is None:
                ax.reasons.append(f"occurrence at ({o.world_pt.x:.3f}, {o.world_pt.y:.3f}): {why}")
            else:
                votes[idx] += 1
        if not votes:
            ax.reasons.append("no occurrence could be associated with a line")
            continue
        ranked = votes.most_common()
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            ax.status = "ambiguous"
            ax.reasons.append(f"tie between {len(ranked)} lines "
                              f"({ranked[0][1]} votes each) from {ax.n_occ} occurrences")
            continue
        idx, n = ranked[0]
        c = cands[idx]
        ax.status, ax.point, ax.dir, ax.length, ax.n_agree = "ok", c.point, c.dir, c.length, n
        if n < ax.n_occ:
            ax.reasons.append(f"{n}/{ax.n_occ} occurrences agree")
        chosen[sfx] = idx

    # two labels on one physical line -> both ambiguous
    by_line: dict[int, list[str]] = {}
    for sfx, idx in chosen.items():
        by_line.setdefault(idx, []).append(sfx)
    for idx, group in by_line.items():
        if len(group) > 1:
            for sfx in group:
                a = axes[sfx]
                a.status = "ambiguous"
                a.reasons.append(f"same line also chosen by {sorted(set(group) - {sfx})}")
                a.point = a.dir = None

    return GridResult(axes=axes, families=_families(axes), extent=ext, prefix=prefix)


def _ang(d: Vec2) -> float:
    return math.degrees(math.atan2(d.y, d.x)) % 180.0


def _adiff(a: float, b: float) -> float:
    x = abs(a - b) % 180.0
    return min(x, 180.0 - x)


def _families(axes: dict) -> tuple:
    have = sorted(k for k, a in axes.items() if a.status == "ok" and a.dir is not None)
    clusters: list[list] = []     # [angle, [labels]]
    for k in have:
        ang = _ang(axes[k].dir)
        for cl in clusters:
            if _adiff(cl[0], ang) <= FAMILY_ANGLE_DEG:
                cl[1].append(k)
                break
        else:
            clusters.append([ang, [k]])
    clusters.sort(key=lambda c: -len(c[1]))
    for extra in clusters[2:]:
        for k in extra[1]:
            axes[k].reasons.append("direction matches neither grid family")
    groups = [sorted(c[1]) for c in clusters[:2]]
    while len(groups) < 2:
        groups.append([])
    # stable order: family 0 contains the alphabetically-first label
    groups.sort(key=lambda g: g[0] if g else "￿")
    return (groups[0], groups[1])


def line_intersection(p1: Vec2, d1: Vec2, p2: Vec2, d2: Vec2) -> Vec2 | None:
    den = d1.x * d2.y - d1.y * d2.x
    if abs(den) < 1e-12:
        return None
    t = ((p2.x - p1.x) * d2.y - (p2.y - p1.y) * d2.x) / den
    return Vec2(p1.x + t * d1.x, p1.y + t * d1.y)


def intersections(result: GridResult) -> dict:
    """{(label_in_family0, label_in_family1): Vec2} for every ok axis pair.

    Family 0 is the family containing the alphabetically-first detected
    label (so digits-vs-letters ordering is by string sort, e.g. "1" < "A").
    """
    f0, f1 = result.families
    out = {}
    for a in f0:
        for b in f1:
            ax, bx = result.axes[a], result.axes[b]
            if ax.status != "ok" or bx.status != "ok":
                continue
            p = line_intersection(ax.point, ax.dir, bx.point, bx.dir)
            if p is not None:
                out[(a, b)] = p
    return out
