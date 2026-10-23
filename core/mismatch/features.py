#!/usr/bin/env python3
"""core/mismatch/features.py — feature extraction (PLAN_MISMATCH §1.1).

One pass over a drawing with :func:`core.grid.walk_world` turns every
piece of recognised geometry into a :class:`Feature` in **world
coordinates**, classified against the policy's families.

What comes out
--------------
``circle``   a centre and a radius (``r is None`` for a hex/octagon nut,
             which is only ever a *centre*), from CIRCLE, a near-full ARC,
             a near-circular ELLIPSE, a 2-vertex bulge circle, a regular
             hexagon/octagon, or an exploded circle of ≥12 vertices.
``rect``     an oriented rectangle: centre, unit axis ``u``, ``w`` along
             ``u``, ``h`` across it — from a closed 4-vertex polyline
             whose corners are 90° **after the transform**, a SOLID/TRACE,
             or four co-closing lines (optional, see below).
``polygon``  any other closed polyline, flattened, with its convexity and
             hull kept for the containment check.
``segment``  a LINE, or a straight edge of an open polyline.
``band``     a beam in plan: centreline ``a``–``b`` and half-width ``hw``,
             from a long member rect, a pair of parallel segment edges, or
             an unpaired long segment (``assumed_width``).
``point``    a POINT entity.
``insert``   the anchor of an INSERT that a *point family* recognises —
             the pipe-support case of §1.1.

The awkward parts, and how they are handled
-------------------------------------------
**Effective layer.**  An entity on layer ``"0"`` inside a block takes the
layer of the nearest enclosing INSERT whose layer is not ``"0"``
(:attr:`core.grid.WorldItem.inherited_layer`).  Display prefixes
``MECH|`` / ``STR|`` / ``STR$0$`` are stripped before any regex sees the
name.

**Layer prefilter.**  ``classify.Classifier.candidates`` memoises
"(layer, block chain) → families that could match" in a dict; an entity
whose lookup is empty is never read.  One lookup, no geometry.

**OCS.**  CIRCLE / ARC / ELLIPSE / LWPOLYLINE / POLYLINE / SOLID / TRACE
go through ``entity.ocs().to_wcs(...)`` before the INSERT matrix, so a
circle whose extrusion is ``(0, 0, -1)`` lands where it really is.

**Non-uniform scale.**  ``sx``/``sy`` are the lengths the world matrix
stretches the local x and y axes to.  If they disagree by more than
1e-6 relative, a *circle* is really an ellipse: it is skipped and counted
in ``skipped["nonuniform"]`` rather than silently reported with a bogus
radius.  Rectangles are not skipped — they simply stop being rectangles
(their corners are no longer 90°), so they degrade into polygons.

**Block cache.**  Geometry is extracted once per *block definition*, keyed
``(block_name, effective_layer_of_parent)``, and mapped to each instance
with one numpy ``local @ M[:2,:2] + M[3,:2]`` per block instance.  The
cache holds the block's features in *its own* coordinates; the layers and
the INSERT chain are resolved per instance, because the same block
inserted from two different parents has two different chains.

Units
-----
Geometry is in drawing units (MECH units for both documents once the STR
file has been mapped with the Step 5 transform).  ``size_mm`` on every
feature is the same geometry multiplied by ``mm_per_unit``, which is the
only thing family size ranges are ever compared against.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
from ezdxf.math import Vec3

from core import grid as _grid

from . import geom
from .classify import Classifier, normalize_layer
from .model import EntityRef
from .policy import DEFAULT_POLICY, MismatchPolicy

__all__ = [
    "Feature",
    "FeatureSet",
    "extract",
    "extract_pair",
    "mm_per_unit_of",
    "normalize_layer",
    "ARC_FULL_SPAN_DEG",
    "ELLIPSE_RATIO_MIN",
    "BULGE_CIRCLE_TOL",
    "REGULAR_RADIAL_TOL",
    "REGULAR_POLY_MIN_N",
    "NUT_N",
    "NONUNIFORM_TOL",
]

# ── thresholds (PLAN_MISMATCH §1.1) ────────────────────────────────────────

#: An ARC closes a circle at this span or beyond (degrees).
ARC_FULL_SPAN_DEG = 350.0
#: An ELLIPSE counts as a circle at this major/minor ratio or above.
ELLIPSE_RATIO_MIN = 0.999
#: ``|bulge|`` tolerance for the 2-vertex circle.
BULGE_CIRCLE_TOL = 1e-6
#: Radial ``std / mean`` under which a closed polygon is "regular".
REGULAR_RADIAL_TOL = 0.02
#: A closed polygon with at least this many vertices is an exploded circle.
REGULAR_POLY_MIN_N = 12
#: Vertex counts that are nuts / bolt heads: centre only, no radius.
NUT_N = (6, 8)
#: Relative sx/sy disagreement above which a circle is really an ellipse.
NONUNIFORM_TOL = 1e-6
#: Chordal tolerance used when flattening bulged polylines, relative to
#: the shape's own diagonal.
FLATTEN_REL_TOL = 1e-3
#: Rect corner tolerance, degrees.
RECT_ANGLE_TOL_DEG = 0.5
#: A rect member becomes a band at this aspect ratio.
BAND_ASPECT = 3.0
#: Beam-band pairing: angular bin width and parallel tolerance, degrees.
BAND_BIN_DEG = 0.5
BAND_PARALLEL_DEG = 0.5
#: Minimum axial overlap of a paired segment pair, as a fraction.
BAND_OVERLAP_FRAC = 0.5

_PROXY_TYPES = {"ACAD_PROXY_ENTITY", "ACAD_PROXY_OBJECT"}
_IGNORED_TYPES = frozenset({
    "TEXT", "MTEXT", "ATTDEF", "ATTRIB", "HATCH", "SPLINE", "ELLIPSE_ARC",
    "POINT_ALT", "IMAGE", "INSERTIONMARKER", "RAY", "XLINE", "MLEADER",
    "LEADER", "MLINE", "DIMENSION", "LEADERGEOM", "ACAD_TABLE",
    "DIMENSION_ORDINATE", "VIEWPORT", "WIPEOUT",
})


# ── the feature ─────────────────────────────────────────────────────────────

@dataclass(eq=False)
class Feature:
    """One classified shape in world coordinates.

    ``size_mm`` holds the world-mapped size in millimetres and is what the
    policy's size ranges see: ``{"r": …}`` for circles (the *mean* radius
    even when ``r is None``, so a hex nut can still be range-checked),
    ``{"w": …, "h": …}`` for rects and polygons, ``{"len": …}`` for
    segments and bands, ``{"w": 0.0}`` for the dimensionless point and
    insert features.
    """

    kind: str                                       # circle|rect|polygon|segment|band|point|insert
    source: str = ""                                # "MECH" | "STR"
    handle: str = ""
    insert_chain: tuple = ()
    block: str = ""                                 # innermost block ("" = modelspace)
    root_block: str = ""                            # outermost block ("" = modelspace)
    layer: str = ""                                 # effective layer, prefixes stripped
    #: raw layer of the entity itself (``None`` = it sits on layer "0" and
    #: inherits :attr:`core.grid.WorldItem.inherited_layer`)
    own_layer: str | None = None
    cx: float = 0.0
    cy: float = 0.0
    r: float | None = None                          # None = centre only (hex/octagon)
    ux: float = 1.0                                 # rect axis / band direction
    uy: float = 0.0
    w: float = 0.0
    h: float = 0.0
    a: tuple = (0.0, 0.0)                           # band / segment start
    b: tuple = (0.0, 0.0)                           # band / segment end
    hw: float = 0.0                                 # band half-width
    points: tuple = ()                              # polygon ring, as a tuple of (x, y)
    straight: bool = False                         # closed polyline had no bulges
    convex: bool | None = None
    assumed_width: bool = False                     # band built from one lonely line
    size_mm: dict = field(default_factory=dict)
    families: tuple = ()
    in_overlay: bool | None = None

    # ── derived geometry ──────────────────────────────────────────
    def centre(self) -> tuple[float, float]:
        return (float(self.cx), float(self.cy))

    def length(self) -> float:
        return math.hypot(self.b[0] - self.a[0], self.b[1] - self.a[1])

    def normal(self) -> tuple[float, float]:
        """Left normal of the feature's ``u`` axis (bands, rects, segments)."""
        return (-float(self.uy), float(self.ux))

    def corners(self) -> np.ndarray:
        """Rect corners, counter-clockwise."""
        return geom.rect_corners(self.centre(), (self.ux, self.uy), self.w, self.h)

    def ring(self) -> np.ndarray:
        """The polygon ring as an ``(N, 2)`` array (empty for other kinds)."""
        return geom.as_points(self.points)

    def bbox(self) -> tuple[float, float, float, float]:
        """``(xmin, ymin, xmax, ymax)`` in drawing units."""
        if self.kind == "circle":
            r = self.r if self.r is not None else 0.0
            return (self.cx - r, self.cy - r, self.cx + r, self.cy + r)
        if self.kind in ("point", "insert"):
            return (self.cx, self.cy, self.cx, self.cy)
        if self.kind in ("band", "segment"):
            (ax, ay), (bx, by) = self.a, self.b
            return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))
        if self.kind == "rect":
            c = self.corners()
            return (float(c[:, 0].min()), float(c[:, 1].min()),
                    float(c[:, 0].max()), float(c[:, 1].max()))
        p = self.ring()
        if len(p) == 0:
            return (self.cx, self.cy, self.cx, self.cy)
        return (float(p[:, 0].min()), float(p[:, 1].min()),
                float(p[:, 0].max()), float(p[:, 1].max()))

    def block_chain(self) -> tuple[str, ...]:
        """Block names to test against a family regex — the chain plus, for
        an INSERT, the name of the block it is about to enter."""
        return self.insert_chain

    # ── identity ───────────────────────────────────────────────────
    def entity_ref(self, family: str) -> EntityRef:
        """The :class:`~core.mismatch.model.EntityRef` for one family."""
        return EntityRef(source=self.source, handle=self.handle,
                         insert_chain=tuple(self.insert_chain),
                         block=self.block, root_block=self.root_block,
                         layer=self.layer, family=family, feature=self.kind,
                         size_mm=dict(self.size_mm), in_overlay=self.in_overlay)

    def entity_refs(self) -> tuple[EntityRef, ...]:
        return tuple(self.entity_ref(f) for f in self.families)

    def sort_key(self) -> tuple:
        """Total order used before anything downstream looks at the set."""
        return (self.source, self.layer, self.kind, self.handle,
                round(self.cx, 6), round(self.cy, 6),
                round(float(self.r if self.r is not None else -1.0), 6),
                round(self.w, 6), round(self.h, 6))

    def __repr__(self) -> str:                       # pragma: no cover - debug aid
        return (f"Feature({self.kind} {self.layer}@{self.cx:.3f},{self.cy:.3f} "
                f"r={self.r} w={self.w:.3f} h={self.h:.3f} fam={self.families})")


@dataclass
class FeatureSet:
    """Everything one drawing contributed, plus what it did not contribute."""

    source: str
    mm_per_unit: float = 1.0
    features: list = field(default_factory=list)
    skipped: dict = field(default_factory=dict)
    units_assumed: bool = False

    def __len__(self) -> int:
        return len(self.features)

    def __iter__(self):
        return iter(self.features)

    def skip(self, key: str, n: int = 1) -> None:
        self.skipped[key] = int(self.skipped.get(key, 0)) + n

    def by_family(self) -> dict[str, list]:
        """``{family: [Feature, …]}`` for every family that matched something.

        Features keep the extraction order inside each family, so a check
        that needs a deterministic order (bolt patterns, band greedy
        pairing) can rely on it.
        """
        out: dict[str, list] = {}
        for f in self.features:
            for fam in f.families:
                out.setdefault(fam, []).append(f)
        return out

    def counts(self) -> dict[str, int]:
        """``{family: n}``; only families with at least one feature appear."""
        return {fam: len(items) for fam, items in self.by_family().items()}

    def bboxes(self) -> np.ndarray:
        """``(N, 4)`` world bounding boxes — feed straight into GridIndex."""
        if not self.features:
            return np.zeros((0, 4), dtype=float)
        return np.asarray([f.bbox() for f in self.features], dtype=float)

    def bbox(self) -> tuple[float, float, float, float]:
        b = self.bboxes()
        if b.size == 0:
            return (0.0, 0.0, 0.0, 0.0)
        return (float(b[:, 0].min()), float(b[:, 1].min()),
                float(b[:, 2].max()), float(b[:, 3].max()))

    def of_kind(self, *kinds: str) -> list:
        want = set(kinds)
        return [f for f in self.features if f.kind in want]

    def sorted(self) -> "FeatureSet":
        """A new FeatureSet with the features in :meth:`Feature.sort_key` order."""
        return FeatureSet(self.source, self.mm_per_unit,
                          sorted(self.features, key=Feature.sort_key),
                          dict(self.skipped), self.units_assumed)


# ── OCS helpers ─────────────────────────────────────────────────────────────

def _ocs_xy(e, p) -> tuple[float, float]:
    """Entity-local ``(x, y)`` → WCS, honouring a non-default extrusion.

    CIRCLE / ARC / ELLIPSE / polylines / SOLID store their plan coordinates
    in an OCS defined by the extrusion vector; with ``extrusion`` of
    ``(0, 0, -1)`` the OCS x axis points the *other* way, so skipping this
    is a real coordinate error, not a formality.
    """
    try:
        ext = e.dxf.extrusion
    except Exception:
        ext = (0.0, 0.0, 1.0)
    x = float(p[0])
    y = float(p[1])
    z = float(p[2]) if len(p) > 2 else 0.0
    if float(ext[0]) == 0.0 and float(ext[1]) == 0.0 and float(ext[2]) == 1.0:
        return (x, y)
    try:
        w = e.ocs().to_wcs(Vec3(x, y, z))
        return (float(w.x), float(w.y))
    except Exception:
        return (x, y)


def _layer_of(e) -> str:
    try:
        return str(e.dxf.layer)
    except Exception:
        return "0"


def _block_of(e) -> str:
    try:
        return str(e.dxf.name)
    except Exception:
        return ""


# ── bulge maths ─────────────────────────────────────────────────────────────

def _bulge_centre(p1, p2, bulge) -> tuple[tuple[float, float], float] | None:
    """``(centre, radius)`` of the arc described by a bulge, or ``None``."""
    b = float(bulge)
    if abs(b) < 1e-12:
        return None
    theta = 4.0 * math.atan(b)
    ax, ay = float(p1[0]), float(p1[1])
    bx, by = float(p2[0]), float(p2[1])
    chord = math.hypot(bx - ax, by - ay)
    half = theta / 2.0
    s = math.sin(half)
    if chord < 1e-15 or abs(s) < 1e-15:
        return None
    r = abs(chord / (2.0 * s))
    ux, uy = geom.unit((bx - ax, by - ay))
    d = math.sqrt(max(r * r - (chord / 2.0) ** 2, 0.0))
    sign = 1.0 if b > 0 else -1.0
    # left normal of p1->p2 is (-uy, ux); a positive (CCW) bulge puts the
    # centre on its *right*.
    return ((0.5 * (ax + bx) + sign * d * uy, 0.5 * (ay + by) - sign * d * ux), r)


def _bulge_arc_points(p1, p2, bulge, tol) -> list[tuple[float, float]]:
    """Flatten one bulged edge into points, endpoints included."""
    got = _bulge_centre(p1, p2, bulge)
    if got is None:
        return [(float(p1[0]), float(p1[1])), (float(p2[0]), float(p2[1]))]
    (ox, oy), r = got
    theta = 4.0 * math.atan(float(bulge))
    ax, ay = float(p1[0]), float(p1[1])
    a0 = math.atan2(ay - oy, ax - ox)
    r_eff = max(r, 1e-12)
    step = math.sqrt(max(8.0 * r_eff * tol, 1e-18))
    n = max(2, int(math.ceil(abs(theta) * r_eff / step)))
    out = []
    for k in range(n + 1):
        a = a0 + theta * (k / n)
        out.append((ox + r_eff * math.cos(a), oy + r_eff * math.sin(a)))
    out[0] = (ax, ay)
    out[-1] = (float(p2[0]), float(p2[1]))
    return out


# ── block-definition extraction (local coordinates) ─────────────────────────

def _polyline_vertices(e):
    """``([(x, y, bulge)], closed, elevation)`` in WCS for a polyline entity."""
    t = e.dxftype()
    if t == "LWPOLYLINE":
        try:
            raw = e.get_points("xyb")
        except Exception:
            return None
        elev = float(e.dxf.elevation) if e.dxf.hasattr("elevation") else 0.0
        pts = [_ocs_xy(e, (p[0], p[1], elev)) for p in raw]
        return pts, [float(p[2]) for p in raw], bool(getattr(e, "closed", False))
    if t == "POLYLINE":
        try:
            if not e.is_2d_polyline:
                return None
            vs = list(e.vertices)
        except Exception:
            return None
        if not vs:
            return None
        elev = float(getattr(e.dxf, "elevation", 0.0) or 0.0)
        pts = [_ocs_xy(e, (v.dxf.location[0], v.dxf.location[1], elev)) for v in vs]
        return pts, [float(v.dxf.get("bulge", 0.0) or 0.0) for v in vs], \
            bool(getattr(e, "is_closed", False))
    return None


def _flatten_closed(pts, bulges, closed, tol) -> list[tuple[float, float]]:
    """Points of a closed polyline, bulging edges flattened away."""
    n = len(pts)
    out: list[tuple[float, float]] = []
    last = n if closed else n - 1
    for i in range(last):
        j = (i + 1) % n
        b = bulges[i] if i < len(bulges) else 0.0
        if abs(b) > 1e-12:
            seg = _bulge_arc_points(pts[i], pts[j], b, tol)
        else:
            seg = [pts[i], pts[j]]
        out.extend(seg[:-1] if out else seg)
    return out


def _local_features(e, block_entity: bool) -> tuple[list, str]:
    """Features of one entity **in its own block-definition coordinates**.

    Returns ``(features, note)`` where ``note`` is a ``skipped`` key or
    ``""``.  Every returned :class:`Feature` carries ``own_layer`` (the raw
    layer, ``None`` for layer "0") and empty chain/block fields; the world
    pass fills those in.
    """
    t = e.dxftype()
    out: list[Feature] = []
    own = _layer_of(e)
    if own == "0":
        own = None

    if t in _PROXY_TYPES:
        return [], "proxy"
    if t in _IGNORED_TYPES:
        return [], "unsupported"

    if t == "INSERT":
        if not block_entity:
            # modelspace / parent-space anchor point of the INSERT
            at = _ocs_xy(e, e.dxf.insert)
            out.append(Feature(kind="insert", own_layer=own, cx=at[0], cy=at[1]))
        return out, ""

    if t == "CIRCLE":
        c = _ocs_xy(e, (e.dxf.center[0], e.dxf.center[1], e.dxf.center[2]))
        out.append(Feature(kind="circle", own_layer=own, cx=c[0], cy=c[1],
                           r=float(e.dxf.radius)))
        return out, ""

    if t == "ARC":
        raw = float(e.dxf.end_angle) - float(e.dxf.start_angle)
        # a full circle is written 0 → 360, whose modulo is 0, so the raw
        # difference has to be checked too
        span = raw % 360.0
        if span < ARC_FULL_SPAN_DEG and raw < ARC_FULL_SPAN_DEG:
            return [], "unsupported"
        c = _ocs_xy(e, (e.dxf.center[0], e.dxf.center[1], e.dxf.center[2]))
        out.append(Feature(kind="circle", own_layer=own, cx=c[0], cy=c[1],
                           r=float(e.dxf.radius)))
        return out, ""

    if t == "ELLIPSE":
        ratio = float(e.dxf.ratio)
        if ratio < ELLIPSE_RATIO_MIN:
            return [], "unsupported"
        c = _ocs_xy(e, (e.dxf.center[0], e.dxf.center[1], e.dxf.center[2]))
        major = e.dxf.major_axis
        r = math.hypot(float(major[0]), float(major[1]))
        out.append(Feature(kind="circle", own_layer=own, cx=c[0], cy=c[1], r=r))
        return out, ""

    if t == "POINT":
        at = _ocs_xy(e, (e.dxf.location[0], e.dxf.location[1], e.dxf.location[2]))
        out.append(Feature(kind="point", own_layer=own, cx=at[0], cy=at[1]))
        return out, ""

    if t == "LINE":
        a = _ocs_xy(e, (e.dxf.start[0], e.dxf.start[1], e.dxf.start[2]))
        b = _ocs_xy(e, (e.dxf.end[0], e.dxf.end[1], e.dxf.end[2]))
        out.append(Feature(kind="segment", own_layer=own, a=a, b=b))
        return out, ""

    if t in ("SOLID", "TRACE"):
        try:
            vs = [tuple(float(c) for c in v) for v in e.vertices()]
        except Exception:
            return [], "unsupported"
        if len(vs) != 4:
            return [], "unsupported"
        # DXF stores SOLID/TRACE corners as 1, 2, 4, 3; the loop is 0-1-3-2
        pts = [(vs[0][0], vs[0][1]), (vs[1][0], vs[1][1]),
               (vs[3][0], vs[3][1]), (vs[2][0], vs[2][1])]
        out.append(Feature(kind="closed_poly", own_layer=own,
                           points=tuple(pts), straight=True))
        return out, ""

    if t in ("LWPOLYLINE", "POLYLINE"):
        got = _polyline_vertices(e)
        if got is None:
            return [], "unsupported"
        pts, bulges, closed = got
        if len(pts) < 2:
            return [], "unsupported"
        if closed:
            if len(pts) == 2 and all(abs(b) > 0 and abs(abs(b) - 1.0) <= BULGE_CIRCLE_TOL
                                     for b in bulges):
                half = 0.5 * (pts[0][0] + pts[1][0]), 0.5 * (pts[0][1] + pts[1][1])
                r = 0.5 * math.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
                out.append(Feature(kind="circle", own_layer=own,
                                   cx=half[0], cy=half[1], r=r))
                return out, ""
            straight = all(abs(b) <= 1e-12 for b in bulges)
            ring = list(pts)
            if not straight:
                span = max(math.hypot(ring[i + 1][0] - ring[i][0],
                                      ring[i + 1][1] - ring[i][1])
                           for i in range(len(ring) - 1))
                tol = max(span * FLATTEN_REL_TOL, 1e-9)
                ring = _flatten_closed(pts, bulges, True, tol)
            out.append(Feature(kind="closed_poly", own_layer=own,
                               points=tuple(ring), straight=straight))
            return out, ""
        # open polyline: straight edges only (§1.1 Segment)
        for i in range(len(pts) - 1):
            if abs(bulges[i] if i < len(bulges) else 0.0) > 1e-12:
                continue
            out.append(Feature(kind="segment", own_layer=own,
                               a=pts[i], b=pts[i + 1]))
        return out, "unsupported" if not out else ""

    return [], "unsupported"


def _local_points(lf: Feature) -> np.ndarray:
    """The local coordinates a feature has to map (0, 1, 2 or N points)."""
    k = lf.kind
    if k == "circle":
        return np.asarray([[lf.cx, lf.cy]], dtype=float)
    if k in ("point", "insert"):
        return np.asarray([[lf.cx, lf.cy]], dtype=float)
    if k == "segment":
        return np.asarray([lf.a, lf.b], dtype=float)
    if k == "closed_poly":
        return geom.as_points(lf.points)
    return np.zeros((0, 2), dtype=float)


# ── the extraction context ──────────────────────────────────────────────────

class _Ctx:
    """Per-call state: classifier, policy knobs, block cache, counters."""

    def __init__(self, source: str, classifier: Classifier, policy: MismatchPolicy,
                 mm_per_unit: float, layout, overlay_handles, fs: FeatureSet) -> None:
        self.source = source
        self.clf = classifier
        self.pol = policy
        self.mm = float(mm_per_unit) if mm_per_unit else 1.0
        self.layout = layout
        self.overlay = overlay_handles
        self.fs = fs
        self.defs: dict[tuple[str, str], dict] = {}

    def block_def(self, doc, block_name: str, inherited: str) -> dict:
        """``{handle: (own_layer, [local features])}`` for one block definition.

        Cached on ``(block_name, effective_layer_of_parent)``: two INSERTs
        of the same block with the same inherited layer share the geometry,
        two INSERTs with *different* layers do not, because the entities
        sitting on layer "0" resolve differently.
        """
        key = (block_name, inherited)
        got = self.defs.get(key)
        if got is not None:
            return got
        if block_name:
            container = doc.blocks.get(block_name)
            inner = True
        else:
            container = self.layout
            inner = False
        out: dict = {}
        if container is not None:
            for e in container:
                lf, _note = _local_features(e, inner)
                if lf:
                    out[_handle_of(e)] = lf
        self.defs[key] = out
        return out


# ── local → world ───────────────────────────────────────────────────────────

def _world_from_local(lf: Feature, pts: np.ndarray, *, ctx: _Ctx, item,
                      A: np.ndarray, T: np.ndarray, sx: float, sy: float) -> list:
    """Turn one block-local feature into 0..1 world features.

    ``pts`` are its local coordinates, already mapped by ``local @ A + T``.
    """
    chain = item.chain
    block = chain[-1] if chain else ""
    root_block = chain[0] if chain else ""
    own = lf.own_layer
    eff = own if own else (item.inherited_layer or "")
    layer = normalize_layer(eff)
    blocks = chain
    if lf.kind == "insert":
        blocks = chain + (_block_of(item.entity),)

    base = dict(source=ctx.source, handle=lf.handle, insert_chain=chain,
                block=block, root_block=root_block, layer=layer, own_layer=own)
    if ctx.overlay is not None:
        base["in_overlay"] = lf.handle in ctx.overlay

    if lf.kind == "circle":
        if sx <= 0.0:
            return []
        if abs(sx - sy) / sx > NONUNIFORM_TOL:
            ctx.fs.skip("nonuniform")
            return []
        scale = math.sqrt(abs(sx * sy))
        f = Feature(kind="circle", cx=float(pts[0, 0]), cy=float(pts[0, 1]),
                    r=float(lf.r) * scale, **base)
        f.size_mm = {"r": float(lf.r) * scale * ctx.mm}
        return _finish(f, ctx, layer, blocks)

    if lf.kind in ("point", "insert"):
        f = Feature(kind=lf.kind, cx=float(pts[0, 0]), cy=float(pts[0, 1]), **base)
        f.size_mm = {"w": 0.0, "h": 0.0}
        return _finish(f, ctx, layer, blocks)

    if lf.kind == "segment":
        f = Feature(kind="segment", a=(float(pts[0, 0]), float(pts[0, 1])),
                    b=(float(pts[1, 0]), float(pts[1, 1])), **base)
        f.cx = 0.5 * (f.a[0] + f.b[0])
        f.cy = 0.5 * (f.a[1] + f.b[1])
        f.length()
        f.ux, f.uy = geom.unit((f.b[0] - f.a[0], f.b[1] - f.a[1]))
        f.size_mm = {"len": math.hypot(f.b[0] - f.a[0], f.b[1] - f.a[1]) * ctx.mm}
        return _finish(f, ctx, layer, blocks)

    if lf.kind != "closed_poly":
        return []

    # ---- closed polyline: circle / rect / polygon, decided *after* the map
    n = len(pts)
    if n < 3:
        return []

    # a hex nut / octagon is a circle centre; an exploded circle is a circle
    got = _regular_ring(pts)
    if got is not None:
        (ccx, ccy), mean_r = got
        f = Feature(kind="circle", cx=ccx, cy=ccy, **base)
        f.size_mm = {"r": mean_r * ctx.mm}
        if n in NUT_N:
            f.r = None                          # centre only, by design
            f.ux, f.uy = 1.0, 0.0
            f.convex = True
        else:
            f.r = mean_r
        f.w = f.h = 2.0 * mean_r
        return _finish(f, ctx, layer, blocks)

    if n == 4 and lf.straight and geom.is_rectangle(pts, angle_tol_deg=RECT_ANGLE_TOL_DEG):
        (ccx, ccy), (ux, uy), w, h = geom.rect_from_corners(pts)
        f = Feature(kind="rect", cx=ccx, cy=ccy, ux=ux, uy=uy, w=w, h=h, **base)
        f.a = (float(pts[0, 0]), float(pts[0, 1]))
        f.b = (float(pts[2, 0]), float(pts[2, 1]))
        f.points = tuple((float(x), float(y)) for x, y in pts)
        f.convex = True
        f.size_mm = {"w": w * ctx.mm, "h": h * ctx.mm}
        return _finish(f, ctx, layer, blocks)

    ring = tuple((float(x), float(y)) for x, y in pts)
    f = Feature(kind="polygon", cx=float(pts[:, 0].mean()), cy=float(pts[:, 1].mean()),
                points=ring, **base)
    f.convex = geom.is_convex(pts)
    w = float(pts[:, 0].max() - pts[:, 0].min())
    h = float(pts[:, 1].max() - pts[:, 1].min())
    f.w, f.h = w, h
    f.ux, f.uy = (1.0, 0.0)
    f.size_mm = {"w": w * ctx.mm, "h": h * ctx.mm}
    return _finish(f, ctx, layer, blocks)


def _regular_ring(pts: np.ndarray):
    """``((cx, cy), mean_radius)`` when the ring is a regular polygon.

    "Regular" is a radial ``std / mean`` below :data:`REGULAR_RADIAL_TOL`,
    which is scale- and rotation-invariant, so it survives any similarity
    the INSERT matrix applies.  Returns ``None`` for anything else.
    """
    n = len(pts)
    if n not in NUT_N and n < REGULAR_POLY_MIN_N:
        return None
    cx = float(pts[:, 0].mean())
    cy = float(pts[:, 1].mean())
    r = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
    mean = float(r.mean())
    if mean < 1e-12:
        return None
    if float(r.std()) / mean > REGULAR_RADIAL_TOL:
        return None
    return (cx, cy), mean


def _finish(f: Feature, ctx: _Ctx, layer: str, blocks: tuple) -> list:
    """Attach the families and return ``[f]`` or ``[]``.

    Two kinds of geometry are kept *without* a family, because §1.1 turns
    them into something that does have one later: **segments** (flange
    edges waiting for their partner) and **member rects** (a beam drawn as
    a closed outline).  No family declares the ``segment`` type, and
    ``str_member`` declares ``band``, so neither would survive extraction
    without this exception.  Whatever does not become a band is dropped at
    the end of :func:`extract`.
    """
    f.families = ctx.clf.assign(source=f.source, feature_type=f.kind,
                                size_mm=f.size_mm, layer=layer, blocks=blocks)
    if f.families:
        return [f]
    if f.kind in ("segment", "rect") and ctx.clf.band_material(f.source, layer, blocks):
        return [f]
    return []


# ── bands (PLAN_MISMATCH §1.1 b) ────────────────────────────────────────────

def _segment_list(feats: list, ctx: _Ctx) -> list:
    """Segments of the drawing that could become part of a member band.

    Deliberately *not* filtered by length: a beam drawn as several short
    edges has to survive until :func:`_pair_segments` has joined them.
    The minimum length of §1.1(c) applies only to what is left unpaired.
    """
    out = []
    for f in feats:
        if f.kind != "segment" or f.length() <= 0.0:
            continue
        if ctx.clf.band_material(ctx.source, f.layer, f.insert_chain):
            out.append(f)
    return out


def _band_families_for(f: Feature, ctx: _Ctx) -> tuple:
    """Band families of ``ctx.source`` that recognise this feature *and*
    whose size range its ``size_mm`` falls inside."""
    return tuple(fam.name for fam in ctx.clf.band_families(ctx.source)
                 if fam.size_ok(f.size_mm))


def _make_band(template: Feature, a, b, hw, *, assumed: bool) -> Feature:
    """Clone ``template`` into a band with the given centreline and width."""
    out = Feature(kind="band", source=template.source, handle=template.handle,
                  insert_chain=template.insert_chain, block=template.block,
                  root_block=template.root_block, layer=template.layer,
                  own_layer=template.own_layer, a=(float(a[0]), float(a[1])),
                  b=(float(b[0]), float(b[1])), hw=float(hw),
                  assumed_width=bool(assumed), in_overlay=template.in_overlay)
    out.cx = 0.5 * (out.a[0] + out.b[0])
    out.cy = 0.5 * (out.a[1] + out.b[1])
    out.ux, out.uy = geom.unit((out.b[0] - out.a[0], out.b[1] - out.a[1]))
    out.size_mm = {}
    return out


def _build_bands(feats: list, ctx: _Ctx) -> tuple[list, list, set]:
    """Turn member geometry into bands.

    Returns ``(kept, bands, absorb)``: the features that stay features (a
    member rect has become a band and is no longer a rect), the bands, and
    the ids of the segments the bands swallowed.
    """
    band_cfg = ctx.pol.band
    mm = ctx.mm
    kept: list[Feature] = []
    made: list[Feature] = []
    absorb: set[int] = set()
    if not ctx.clf.has_band_families(ctx.source):
        return list(feats), [], absorb

    # ── (a) a member rect with aspect >= 3 is a band already
    for f in feats:
        if f.kind != "rect" or not _is_member_rect(f, ctx):
            kept.append(f)
            continue
        along = max(f.w, f.h)
        across = min(f.w, f.h)
        u = (f.ux, f.uy) if f.w >= f.h else (-f.uy, f.ux)
        p0, p1 = geom.segment_axis_params(
            (f.cx - f.ux * f.w / 2.0, f.cy - f.uy * f.w / 2.0),
            (f.cx + f.ux * f.w / 2.0, f.cy + f.uy * f.w / 2.0),
            (f.cx, f.cy), u)
        band = _make_band(f,
                          (f.cx + u[0] * p0, f.cy + u[1] * p0),
                          (f.cx + u[0] * p1, f.cy + u[1] * p1), 0.5 * across,
                          assumed=False)
        band.size_mm = {"len": along * mm, "w": across * mm, "h": across * mm}
        band.families = _band_families(band, ctx)
        (made if band.families else kept).append(band if band.families else f)

    # ── (b) parallel segment pairs, largest gap first
    segs = _segment_list(feats, ctx)
    used: set[int] = set()
    for band, i, j in _pair_segments(segs, ctx):
        absorb.add(id(segs[i]))
        absorb.add(id(segs[j]))
        used.add(i)
        used.add(j)
        fams: set[str] = set()
        for k in (i, j):
            fams.update(_band_families(segs[k], ctx))
        band.families = _band_families(band, ctx)
        if band.families:
            made.append(band)

    # ── (c) a lonely long segment: assume a default flange width
    half_units = 0.5 * float(band_cfg.default_flange_mm) / (mm or 1.0)
    min_len_units = float(band_cfg.min_member_len_mm) / (mm or 1.0)
    for i, f in enumerate(segs):
        if i in used or f.length() < min_len_units:
            continue
        band = _make_band(f, f.a, f.b, half_units, assumed=True)
        band.size_mm = {"len": f.length() * mm,
                        "w": 2.0 * band.hw * mm, "h": 2.0 * band.hw * mm}
        band.families = _band_families(band, ctx)
        if band.families:
            made.append(band)
        used.add(i)
    return kept, made, absorb


def _is_member_rect(f: Feature, ctx: _Ctx) -> bool:
    """A rect a band family recognises *and* that is long and thin."""
    if not _band_families_for_rect(f, ctx):
        return False
    across = min(f.w, f.h)
    return max(f.w, f.h) >= BAND_ASPECT * max(across, 1e-12) and across > 0.0


def _band_families_for_rect(f: Feature, ctx: _Ctx) -> bool:
    """Does any band family recognise this rect's layer / block chain?"""
    band_fams = {fam.name for fam in ctx.clf.band_families(ctx.source)}
    return bool(band_fams & set(ctx.clf.candidates(f.layer, f.insert_chain)))


def _band_families(f: Feature, ctx: _Ctx) -> tuple:
    return _band_families_for(f, ctx)


def _pair_segments(segs: list, ctx: _Ctx) -> list:
    """Greedy pairing of parallel segments into bands — largest gap first.

    §1.1(b): bucket the segments by direction in 0.5° bins (adjacent bins
    are searched too, so a partner that straddles a bin edge is still
    found), qualify a pair by parallelity, a perpendicular gap inside
    ``[band.min_mm, band.max_mm]`` and an axial overlap of at least half
    the shorter segment, then accept pairs in decreasing gap so the outer
    flange edges pair up before the web lines.
    """
    if len(segs) < 2:
        return []
    mm = ctx.mm or 1.0
    cfg = ctx.pol.band
    gap_lo = float(cfg.min_mm) / mm
    gap_hi = float(cfg.max_mm) / mm

    dirs = []
    for f in segs:
        ux, uy = geom.unit((f.b[0] - f.a[0], f.b[1] - f.a[1]))
        ang = math.degrees(math.atan2(uy, ux)) % 180.0
        if ux < -1e-12 or (abs(ux) <= 1e-12 and uy < 0.0):     # canonical sign
            ang = (ang + 180.0) % 180.0
        dirs.append((ux, uy, ang, (0.5 * (f.a[0] + f.b[0]), 0.5 * (f.a[1] + f.b[1]))))

    nbins = max(1, int(round(180.0 / BAND_BIN_DEG)))
    buckets: dict[int, list[int]] = {}
    for i, (_ux, _uy, ang, _c) in enumerate(dirs):
        b = int(ang / BAND_BIN_DEG) % nbins
        buckets.setdefault(b, []).append(i)

    pairs = []
    for i in range(len(segs)):
        ux, uy, ang, ci = dirs[i]
        bi = int(ang / BAND_BIN_DEG)
        for step in (-1, 0, 1):
            for j in buckets.get((bi + step) % nbins, ()):
                if j <= i:
                    continue
                ju, jv, jang, cj = dirs[j]
                delta = abs((ang - jang + 90.0) % 180.0 - 90.0)
                if delta > BAND_PARALLEL_DEG:
                    continue
                nx, ny = -uy, ux
                signed_gap = (cj[0] - ci[0]) * nx + (cj[1] - ci[1]) * ny
                gap = abs(signed_gap)
                if gap < gap_lo or gap > gap_hi:
                    continue
                t1 = geom.segment_axis_params(segs[i].a, segs[i].b, ci, (ux, uy))
                t2 = geom.segment_axis_params(segs[j].a, segs[j].b, ci, (ux, uy))
                overlap = min(t1[1], t2[1]) - max(t1[0], t2[0])
                shorter = min(segs[i].length(), segs[j].length())
                if shorter <= 0.0 or overlap < BAND_OVERLAP_FRAC * shorter:
                    continue
                sgn = 1.0 if signed_gap > 0.0 else -1.0
                pairs.append((gap, i, j, ci, (ux, uy), t1, t2, sgn))

    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
    out = []
    used: set[int] = set()
    for gap, i, j, ci, (ux, uy), t1, t2, sgn in pairs:
        if i in used or j in used:
            continue
        used.add(i)
        used.add(j)
        tmin = min(t1[0], t2[0])
        tmax = max(t1[1], t2[1])
        # the band's centreline runs *between* the two edges, not along one
        # of them — otherwise every member would be reported half a width off
        nx, ny = -uy, ux
        mid = sgn * gap * 0.5
        a = (ci[0] + ux * tmin + nx * mid, ci[1] + uy * tmin + ny * mid)
        b = (ci[0] + ux * tmax + nx * mid, ci[1] + uy * tmax + ny * mid)
        band = _make_band(segs[i], a, b, 0.5 * gap, assumed=False)
        band.size_mm = {"len": band.length() * mm,
                        "w": gap * mm, "h": gap * mm}
        out.append((band, i, j))
    return out


# ── rect reconstruction from four lines (optional) ──────────────────────────

def _qkey(p) -> tuple[int, int]:
    return (int(round(float(p[0]) * 1e6)), int(round(float(p[1]) * 1e6)))


def _reconstruct_rects(segs: list, ctx: _Ctx) -> tuple[list, set]:
    """Four co-closing straight segments of one block instance → one rect.

    §1.1's optional ``reconstruct_rects_from_lines``: only entities that
    already passed the layer prefilter take part, and only families that
    accept a ``rect`` can claim the result.  The handle of a reconstructed
    rectangle is the ``+``-joined list of its four source handles, so a
    finding on it still names the real entities.  Returns
    ``(rects, consumed_segment_ids)``.
    """
    absorb: set[int] = set()
    made: list[Feature] = []
    if not ctx.pol.reconstruct_rects_from_lines:
        return made, absorb
    if not ctx.clf.families(ctx.source, "rect"):
        return made, absorb
    groups: dict[tuple, list] = {}
    for f in segs:
        groups.setdefault((f.insert_chain, f.layer), []).append(f)
    for key in sorted(groups, key=lambda k: (k[0], k[1])):
        lst = groups[key]
        if len(lst) < 4:
            continue
        before = len(made)
        made.extend(_find_rect_loops(lst, ctx))
        if len(made) > before:
            absorb.update(id(f) for f in lst)
    return made, absorb


def _find_rect_loops(lst: list, ctx: _Ctx) -> list:
    """Every 4-segment rectangular loop inside one group of segments."""
    by_start: dict[tuple, list[int]] = {}
    for i, f in enumerate(lst):
        by_start.setdefault(_qkey(f.a), []).append(i)
    found: list[Feature] = []
    used: set[int] = set()
    for i, s0 in enumerate(lst):
        if i in used:
            continue
        stack = [(s0.b, (i,), (s0.a, s0.b), 1)]
        while stack:
            pt, path, ring, depth = stack.pop()
            if depth == 4:
                if _qkey(pt) == _qkey(s0.a) and len(set(path)) == 4:
                    f = _rect_from_loop(lst, path, ring, ctx)
                    if f is not None:
                        found.append(f)
                        used.update(path)
                continue
            for j in by_start.get(_qkey(pt), ()):
                if j in path:
                    continue
                nxt = lst[j].b
                stack.append((nxt, path + (j,), ring + (nxt,), depth + 1))
    return [f for f in found if f.families]


def _rect_from_loop(lst, path, ring, ctx: _Ctx) -> Feature | None:
    pts = np.asarray(ring, dtype=float)
    if len(pts) != 4 or not geom.is_rectangle(pts):
        return None
    (cx, cy), (ux, uy), w, h = geom.rect_from_corners(pts)
    tmpl = lst[path[0]]
    handle = "+".join(lst[p].handle for p in path)
    f = Feature(kind="rect", source=ctx.source, handle=handle,
                insert_chain=tmpl.insert_chain, block=tmpl.block,
                root_block=tmpl.root_block, layer=tmpl.layer,
                own_layer=tmpl.own_layer, cx=cx, cy=cy, ux=ux, uy=uy, w=w, h=h,
                a=(float(pts[0, 0]), float(pts[0, 1])),
                b=(float(pts[2, 0]), float(pts[2, 1])),
                points=tuple((float(x), float(y)) for x, y in pts),
                convex=True, in_overlay=tmpl.in_overlay)
    f.size_mm = {"w": w * ctx.mm, "h": h * ctx.mm}
    f.families = ctx.clf.assign(source=ctx.source, feature_type="rect",
                                size_mm=f.size_mm, layer=f.layer,
                                blocks=f.insert_chain)
    return f


# ── the walk ────────────────────────────────────────────────────────────────

def _emit_group(doc, items: list, ctx: _Ctx) -> None:
    """Extract and map one block instance (a run of identical WorldItems)."""
    it0 = items[0]
    clf = ctx.clf
    chain = it0.chain
    block_name = chain[-1] if chain else ""

    # 1 — layer prefilter, one dict lookup per entity
    survivors = []
    for it in items:
        e = it.entity
        t = e.dxftype()
        if t in _PROXY_TYPES:
            ctx.fs.skip("proxy")
            continue
        if t in _IGNORED_TYPES:
            ctx.fs.skip("unsupported")
            continue
        own = _layer_of(e)
        eff = own if own != "0" else it.inherited_layer
        blocks = chain + (_block_of(e),) if t == "INSERT" else chain
        if not clf.candidates(normalize_layer(eff), blocks):
            ctx.fs.skip("prefilter")
            continue
        if t == "INSERT" and not clf.is_point_candidate(normalize_layer(eff), blocks):
            continue
        survivors.append(it)
    if not survivors:
        return

    # 2 — the block-definition cache
    defn = ctx.block_def(doc, block_name, it0.inherited_layer)

    # 3 — map every surviving local feature with one numpy multiply-add
    m = it0.m
    # ``local @ A + T`` has to be ezdxf's own ``m.transform(local)``, and ezdxf
    # is row-vector: ``out = (x·m00 + y·m10, x·m01 + y·m11)``.  So ``A`` is
    # ``M[:2, :2]`` **transposed** — writing it the other way round silently
    # mirrors every rotation in the drawing (an INSERT turned +30° comes out
    # −30°, and the STR→MECH transform comes out reflected).
    A = np.asarray([[m[0, 0], m[0, 1]], [m[1, 0], m[1, 1]]], dtype=float)
    T = np.asarray([float(m[3, 0]), float(m[3, 1])], dtype=float)
    sx = math.hypot(A[0, 0], A[0, 1])       # length of the local x axis
    sy = math.hypot(A[1, 0], A[1, 1])       # length of the local y axis

    jobs = []                      # (local, item, npoints, offset)
    chunks: list[np.ndarray] = []
    total = 0
    for it in survivors:
        for lf in defn.get(_handle_of(it.entity), ()):
            arr = _local_points(lf)
            if len(arr) == 0:
                continue
            jobs.append((lf, it, len(arr), total))
            chunks.append(arr)
            total += len(arr)
    if not jobs:
        return
    local = np.vstack(chunks) if len(chunks) > 1 else chunks[0]
    world = local @ A + T
    for lf, it, n, off in jobs:
        ctx.fs.features.extend(
            _world_from_local(lf, world[off:off + n], ctx=ctx, item=it,
                              A=A, T=T, sx=sx, sy=sy))


def _handle_of(e) -> str:
    try:
        return str(e.dxf.handle)
    except Exception:
        return ""


# ── public API ──────────────────────────────────────────────────────────────

def mm_per_unit_of(doc) -> tuple[float, bool]:
    """``(mm_per_unit, assumed)`` from ``$INSUNITS`` (PLAN_MISMATCH key 2).

    ``$INSUNITS == 0`` (unitless) is assumed to be millimetres and the flag
    is raised so the caller can warn ``units_assumed_mm``.
    """
    try:
        code = int(doc.header.get("$INSUNITS", 0))
    except Exception:
        code = 0
    if code == 0:
        return 1.0, True
    try:
        import ezdxf
        f = float(ezdxf.units.conversion_factor(code, 4))   # 4 == millimetres
    except Exception:
        return 1.0, True
    if not math.isfinite(f) or f <= 0.0:
        return 1.0, True
    return f, False


def extract(doc, *, source: str, policy: MismatchPolicy | None = None,
            classifier: Classifier | None = None,
            mm_per_unit: float = 1.0, root=None, layout=None,
            max_depth: int = 16, overlay_handles: Iterable[str] | None = None,
            bands: bool = True) -> FeatureSet:
    """Extract and classify every recognised feature of one drawing.

    Parameters
    ----------
    doc
        the ezdxf document.
    source
        ``"MECH"`` or ``"STR"`` — which half of the family table applies.
    policy / classifier
        a :class:`~core.mismatch.policy.MismatchPolicy` and/or a
        pre-built :class:`~core.mismatch.classify.Classifier` (passing a
        classifier lets several documents share one prefilter cache).
    mm_per_unit
        millimetres per drawing unit (:func:`mm_per_unit_of`).  Every
        ``size_mm`` and every mm threshold in the band rules goes through it.
    root
        matrix applied before anything else — the Step 5 transform that maps
        the STR drawing into MECH world.  ``None`` means the identity.
    bands
        set ``False`` to skip the band pass entirely.

    Returns a :class:`FeatureSet` sorted by :meth:`Feature.sort_key`.
    """
    pol = policy if policy is not None else (classifier.policy if classifier else DEFAULT_POLICY)
    clf = classifier if classifier is not None else Classifier(pol)
    if root is not None and not hasattr(root, "__getitem__") and hasattr(root, "to_matrix44"):
        root = root.to_matrix44()        # an Affine2D straight out of transform.json
    lay = doc.modelspace() if layout is None else layout
    fs = FeatureSet(source=source, mm_per_unit=float(mm_per_unit) or 1.0)
    ctx = _Ctx(source, clf, pol, mm_per_unit, lay,
               set(overlay_handles) if overlay_handles is not None else None, fs)

    cur_key = None
    group: list = []
    for it in _grid.walk_world(doc, lay, root=root, max_depth=max_depth):
        key = (it.chain, it.handles)
        if key != cur_key:
            if group:
                _emit_group(doc, group, ctx)
            cur_key, group = key, [it]
        else:
            group.append(it)
    if group:
        _emit_group(doc, group, ctx)

    if bands:
        _post_bands(fs, ctx)

    # A segment only ever survives extraction to become part of a band; if
    # it never found a partner and was too short to be a member on its own,
    # it is not a feature of any family and must not reach the checks.
    fs.features = [f for f in fs.features if f.families]
    fs.features.sort(key=Feature.sort_key)
    return fs


def _post_bands(fs: FeatureSet, ctx: _Ctx) -> None:
    """Reconstructed rects and every segment that belongs to a member band.

    Order matters: four-line rectangles first (a beam drawn as four lines
    becomes a rect here and a band in ``_build_bands``), then member rects,
    then segment pairs, then lonely segments.  Segments swallowed by a band
    are removed — a beam's flange edges are the band, not loose segments.
    """
    segs = [f for f in fs.features if f.kind == "segment"]
    made_rects, absorb = _reconstruct_rects(segs, ctx)
    rest = [f for f in fs.features if id(f) not in absorb]
    kept, made_bands, absorb2 = _build_bands(rest, ctx)
    absorb |= absorb2
    fs.features = kept + made_rects + made_bands
    if absorb:
        fs.features = [f for f in fs.features if id(f) not in absorb]


def extract_pair(mech_doc, str_doc, *, affine=None, policy: MismatchPolicy | None = None,
                 classifier: Classifier | None = None, mm_per_unit: float | None = None,
                 mech_root=None, str_root=None, mech_layout=None, str_layout=None,
                 bands: bool = True) -> tuple[FeatureSet, FeatureSet]:
    """``(mech_features, str_features)``, both in MECH world coordinates.

    ``mm_per_unit`` defaults to the **MECH** document's own ``$INSUNITS``
    (:func:`mm_per_unit_of`) — every mm tolerance in the plan is a MECH
    tolerance.  ``str_root`` defaults to ``affine.to_matrix44()``, the Step 5
    transform, which is what ``walk_world(root=…)`` exists for.
    """
    pol = policy if policy is not None else (classifier.policy if classifier else DEFAULT_POLICY)
    clf = classifier if classifier is not None else Classifier(pol)
    mmpu, assumed = mm_per_unit_of(mech_doc)
    if mm_per_unit is not None:
        mmpu = float(mm_per_unit)
    root = str_root
    if root is None and affine is not None:
        root = affine.to_matrix44()
    mech = extract(mech_doc, source="MECH", policy=pol, classifier=clf,
                   mm_per_unit=mmpu, root=mech_root, layout=mech_layout, bands=bands)
    mech.units_assumed = assumed
    strf = extract(str_doc, source="STR", policy=pol, classifier=clf,
                   mm_per_unit=mmpu, root=root, layout=str_layout, bands=bands)
    strf.units_assumed = assumed
    return mech, strf