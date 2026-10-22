#!/usr/bin/env python3
"""synth.py — synthetic MECH + STR drawing-pair generator with exact ground truth.

Content is authored once in a "project frame" P (millimetres).  The MECH
file is P translated by ``mech_offset``; the STR file is P translated by
``str_offset``, rotated by ``-str_rotation_deg`` and scaled by ``u``
(1 for mm, 0.001 for m).  Only modelspace entities are transformed, block
definitions are left untouched so INSERT composition is exercised.

Ground truth (STR -> MECH) is the exact inverse composition::

    p_mech = (1/u) * R(+theta) * p_str + (mech_offset - str_offset)

Conventions
-----------
* ``intersections_*`` are keyed ``(x_label, y_label)`` == ``(letter, digit)``
  regardless of ``letters_on_y``; the value is the grid intersection in that
  file's own coordinates (Vec2).  They are computed analytically (cos/sin),
  independent of ezdxf, and reflect axis shifts / noise actually drawn.
* ``stray_str_entity_at`` is given in the STR *file's own* coordinates.
* ``grid_p`` maps label -> (point, unit_dir) in frame P (nominal positions);
  ``grid_mech`` / ``grid_str`` give the same in each file's coordinates
  (actually drawn positions, i.e. including shifts / noise).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import ezdxf
from ezdxf import transform as ez_transform
from ezdxf.math import Matrix44, Vec2

# Layer names used by the generator (tests refer to these).
LAYER_MECH_GRID = "CENTER"
LAYER_MECH_BUBBLE = "GRID-BUBBLE"
LAYER_STR_GRID = "CENTER"
LAYER_COLUMN = "STEEL-COL"
LAYER_BEAM = "FRAME-STEEL"
LAYER_FOOTING = "CIVIL-FOUNDATION"
LAYER_PLATE = "STEEL-PLATE"
LAYER_FILL = "STEEL-FILL"
LAYER_REV = "REV-A"

# ── mismatch-scene layers (step 8b.1) ──────────────────────────────────────
LAYER_BOLT = "BOLT"                    # MECH bolt circles
LAYER_BOLT_HOLE = "STEEL-HOLE"         # STR hole INSERTs; the circles inside are layer 0
LAYER_BASEPLATE = "BASEPLATE"         # MECH baseplate holes
LAYER_BASEPLATE_ENV = "PLT-ENV"        # the baseplate envelope — deliberately NOT a family
LAYER_SADDLE = "SADDLE"                # MECH beam saddles
LAYER_SKID = "EQUIP-SKID"              # MECH skid footprint
LAYER_EQUIP = "EQUIPMENT"              # MECH equipment envelope
LAYER_ANCHOR = "ANCHOR-BOLT"           # STR anchors
LAYER_PIPE_SUP = "PIPE-SUPPORT"        # MECH pipe-support points

GRID_EXT = 2000.0      # mm the grid lines overshoot the outermost axis
LABEL_OFF = 300.0      # mm label offset beyond the line end
BLOCK_BASE = (100.0, 0.0)  # non-zero base point for rotated GridLine blocks


@dataclass
class GroundTruth:
    """Similarity STR -> MECH: scale, then z-rotate, then translate."""
    scale: float
    rotation_deg: float
    tx: float
    ty: float

    def matrix(self) -> Matrix44:
        return Matrix44.chain(
            Matrix44.scale(self.scale, self.scale, self.scale),
            Matrix44.z_rotate(math.radians(self.rotation_deg)),
            Matrix44.translate(self.tx, self.ty, 0.0),
        )

    def as_affine(self) -> tuple[float, float, float, float, float, float]:
        """(a, b, c, d, tx, ty): x' = a x + b y + tx ; y' = c x + d y + ty."""
        c = self.scale * math.cos(math.radians(self.rotation_deg))
        s = self.scale * math.sin(math.radians(self.rotation_deg))
        return (c, -s, s, c, self.tx, self.ty)

    def apply(self, xy):
        """Map one (x, y) -> (x, y) tuple, or an (N, 2) array -> (N, 2) array."""
        a, b, c, d, tx, ty = self.as_affine()
        arr = np.asarray(xy, dtype=float)
        if arr.ndim == 1:
            return (a * arr[0] + b * arr[1] + tx, c * arr[0] + d * arr[1] + ty)
        out = np.empty_like(arr[:, :2])
        out[:, 0] = a * arr[:, 0] + b * arr[:, 1] + tx
        out[:, 1] = c * arr[:, 0] + d * arr[:, 1] + ty
        return out


@dataclass
class SynthOptions:
    prefix: str = "TP104-"
    x_labels: tuple = ("A", "B", "C", "D")
    y_labels: tuple = ("1", "2", "3")
    x_spacing: tuple = (6000.0, 7500.0, 6000.0)
    y_spacing: tuple = (5000.0, 5000.0)
    mech_offset: tuple = (208379.86, -179584.52)
    str_offset: tuple = (12345.678, -8765.432)   # t_s, frame-P mm
    str_rotation_deg: float = 0.0
    str_units: str = "mm"                        # "mm" | "m"
    rotated_grid_inserts: bool = False
    letters_on_y: bool = False
    duplicate_labels: bool = True
    mtext_label: str | None = "D"
    attrib_label: str | None = "3"
    shift_str_axis: dict = field(default_factory=dict)   # label -> mm along normal
    noise_mm: float = 0.0
    seed: int = 12345
    decoy_line: bool = True
    stray_str_entity_at: tuple | None = None
    mech_name: str = "zz_plant.dxf"
    str_name: str = "aa_frame.dxf"

    # ── mismatch scene (step 8b.1) ──────────────────────────────────
    # Which of ALL_MISMATCH_CASES to draw.  Empty = the base drawing only.
    mismatch_cases: tuple = ()
    # Per-case numeric overrides, e.g.
    #   {"bolt_offset": {"bolt_offset_mm": 12.0, "bolt_offset_deg": 50.0}}
    # Keys are documented on each case builder; unknown keys are ignored.
    mismatch_params: dict = field(default_factory=dict)
    # Extra translation applied to MECH scene entities only, in frame P.
    mismatch_global_shift_mm: tuple = (0.0, 0.0)
    # The scene lives well outside the grid bbox so it can never be
    # mistaken for grid geometry (see the module docstring).
    scene_origin: tuple = (30000.0, 0.0)
    scene_pitch: float = 6000.0
    # Draw the scene INSERTs in reverse order (order-independence test).
    scene_reverse_order: bool = False


LEGACY_COMPATIBLE = SynthOptions(
    str_rotation_deg=0.0, str_units="mm", rotated_grid_inserts=False,
    mtext_label=None, attrib_label=None, decoy_line=False,
)
HARD = SynthOptions(
    str_rotation_deg=7.5, str_units="m", rotated_grid_inserts=True,
    mtext_label="D", attrib_label="3", decoy_line=True,
)


@dataclass(frozen=True)
class ExpectedFinding:
    """Ground truth for one synthetic defect (step 8b.1).

    ``location_mech`` is in **MECH world coordinates, drawing units**.
    The MECH file is only ever translated (by ``mech_offset``), so
    ``location_mech = P + mech_offset`` for a point authored at ``P`` in
    frame P, and ``offset_mm`` is already in the MECH axes.
    """
    case: str
    kind: str
    severity: str
    location_mech: tuple
    magnitude_mm: float | None = None
    offset_mm: tuple | None = None
    rotation_deg: float | None = None
    local_magnitude_mm: float | None = None
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ControlRegion:
    """A disc around which *nothing* may be reported (``case`` groups them)."""
    case: str
    center_mech: tuple
    radius_mm: float


@dataclass
class SynthPair:
    mech_path: Path
    str_path: Path
    truth: GroundTruth
    opts: SynthOptions
    grid_p: dict
    intersections_mech: dict
    intersections_str: dict
    grid_mech: dict
    grid_str: dict
    mismatch_expected: tuple = ()          # tuple[ExpectedFinding, ...]
    mismatch_controls: tuple = ()          # tuple[ControlRegion, ...]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _cum(spacing) -> list[float]:
    out = [0.0]
    for s in spacing:
        out.append(out[-1] + float(s))
    return out


def _rot(v, deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (c * v[0] - s * v[1], s * v[0] + c * v[1])


def _axes(opts: SynthOptions):
    """Return (vertical, horizontal): lists of (label, position_mm).

    Vertical lines are x = pos; horizontal lines are y = pos.
    """
    xs = list(zip(opts.x_labels, _cum(opts.x_spacing)))
    ys = list(zip(opts.y_labels, _cum(opts.y_spacing)))
    return (ys, xs) if opts.letters_on_y else (xs, ys)


def _grid_positions(opts: SynthOptions, drawn: bool):
    """Axis positions in P; drawn=True adds shift/noise (STR only)."""
    vert, horiz = _axes(opts)
    if not drawn:
        return vert, horiz
    rng = np.random.default_rng(opts.seed)

    def adj(lst):
        out = []
        for lab, pos in lst:
            d = float(opts.shift_str_axis.get(lab, 0.0))
            if opts.noise_mm:
                d += float(rng.normal(0.0, opts.noise_mm))
            out.append((lab, pos + d))
        return out
    return adj(vert), adj(horiz)


def _intersections(opts, vert, horiz):
    """{(x_label, y_label): (x, y)} in P for the given axis positions."""
    out = {}
    for vl, vx in vert:
        for hl, hy in horiz:
            key = (hl, vl) if opts.letters_on_y else (vl, hl)
            out[key] = (vx, hy)
    return out


def _span(opts):
    vert, horiz = _axes(opts)
    x0 = min(p for _, p in vert) - GRID_EXT
    x1 = max(p for _, p in vert) + GRID_EXT
    y0 = min(p for _, p in horiz) - GRID_EXT
    y1 = max(p for _, p in horiz) + GRID_EXT
    return x0, x1, y0, y1


def _ensure_layers(doc):
    names = set()
    for layout in [doc.modelspace(), *doc.blocks]:
        for e in layout:
            names.add(e.dxf.layer)
    for n in sorted(names):
        if n not in doc.layers:
            doc.layers.add(n)


def _new_doc(units_code: int):
    doc = ezdxf.new("R2013")
    doc.header["$INSUNITS"] = units_code
    return doc


# ---------------------------------------------------------------------------
# MECH content (frame P)
# ---------------------------------------------------------------------------

def _populate_mech(doc, opts: SynthOptions):
    msp = doc.modelspace()
    vert, horiz = _grid_positions(opts, drawn=False)
    x0, x1, y0, y1 = _span(opts)
    mtext = opts.mtext_label

    def label(text_lab, pt):
        txt = f"{opts.prefix}{text_lab}"
        if mtext is not None and text_lab == mtext:
            msp.add_mtext(txt, dxfattribs={"insert": (pt[0], pt[1], 0), "char_height": 250.0,
                                           "layer": "GRID-TEXT"})
        else:
            msp.add_text(txt, dxfattribs={"insert": (pt[0], pt[1], 0), "height": 250.0,
                                          "layer": "GRID-TEXT"})
        msp.add_circle(pt, 450.0, dxfattribs={"layer": LAYER_MECH_BUBBLE})

    for lab, x in vert:
        msp.add_line((x, y0), (x, y1), dxfattribs={"layer": LAYER_MECH_GRID})
        label(lab, (x, y1 + LABEL_OFF))
        if opts.duplicate_labels:
            label(lab, (x, y0 - LABEL_OFF))
    for lab, y in horiz:
        msp.add_line((x0, y), (x1, y), dxfattribs={"layer": LAYER_MECH_GRID})
        label(lab, (x0 - LABEL_OFF, y))
        if opts.duplicate_labels:
            label(lab, (x1 + LABEL_OFF, y))

    if opts.decoy_line:
        # Long line whose midpoint sits ~200 mm from the first vertical axis's
        # top label, far nearer than that axis's own midpoint.
        vx = vert[0][1]
        msp.add_line((vx - 8000, y1 + LABEL_OFF - 200), (vx + 8000, y1 + LABEL_OFF - 200),
                     dxfattribs={"layer": LAYER_MECH_GRID})

    # Equipment block with every curve type.
    eq = doc.blocks.new("A$C1234")
    eq.add_circle((0, 0), 400.0, dxfattribs={"layer": "EQUIP"})
    eq.add_arc((0, 0), 700.0, 20.0, 160.0, dxfattribs={"layer": "EQUIP"})
    eq.add_ellipse((0, 0), major_axis=(900, 0, 0), ratio=0.5, dxfattribs={"layer": "EQUIP"})
    eq.add_lwpolyline([(-500, -500, 0.0), (500, -500, 0.5), (500, 500, 0.0), (-500, 500, -0.3)],
                      format="xyb", dxfattribs={"layer": "EQUIP"})
    eq.add_spline(fit_points=[(-800, 0), (-300, 400), (300, -400), (800, 0)],
                  dxfattribs={"layer": "EQUIP"})
    h = eq.add_hatch(color=2, dxfattribs={"layer": "EQUIP"})
    ep = h.paths.add_edge_path()
    ep.add_line((1000, -200), (1400, -200))
    ep.add_arc((1400, 0), 200.0, 270.0, 90.0, ccw=True)
    ep.add_line((1400, 200), (1000, 200))
    ep.add_arc((1000, 0), 200.0, 90.0, 270.0, ccw=True)
    eq.add_text("EQ-1234", dxfattribs={"insert": (-300, -900, 0), "height": 150.0, "layer": "EQUIP"})
    msp.add_blockref("A$C1234", (3000, 2500), dxfattribs={"rotation": 0.0, "layer": "EQUIP"})
    msp.add_blockref("A$C1234", (9750, 7500), dxfattribs={"rotation": 30.0, "layer": "EQUIP"})

    # Block TAG: 100 x 100 square.
    tag = doc.blocks.new("TAG")
    tag.add_lwpolyline([(0, 0), (100, 0), (100, 100), (0, 100)], close=True,
                       dxfattribs={"layer": "EQUIP"})
    msp.add_blockref("TAG", (1000, 1000), dxfattribs={"layer": "EQUIP"})

    # Title border and revision triangle.
    msp.add_lwpolyline([(x0 - 2500, y0 - 2500), (x1 + 2500, y0 - 2500),
                        (x1 + 2500, y1 + 2500), (x0 - 2500, y1 + 2500)],
                       close=True, dxfattribs={"layer": "TITLE"})
    msp.add_lwpolyline([(0, y1 + 1500), (400, y1 + 1500), (200, y1 + 1850)],
                       close=True, dxfattribs={"layer": LAYER_REV})


# ---------------------------------------------------------------------------
# STR content (frame P)
# ---------------------------------------------------------------------------

def _gridline_block(doc, opts, label_text, tag_idx, length, vertical, rotated, use_attrib):
    """Create block GridLine-<label>; return (block name, base, rotation)."""
    name = f"GridLine-{label_text}"
    blk = doc.blocks.new(name)
    txt = f"{opts.prefix}{label_text}"
    dup = opts.duplicate_labels
    if rotated:
        # Defined horizontally (local +x = line direction); non-zero base point.
        blk.block.dxf.base_point = (BLOCK_BASE[0], BLOCK_BASE[1], 0.0)
        blk.add_line((0, 0), (length, 0), dxfattribs={"layer": LAYER_STR_GRID})
        p_start, p_end = (-LABEL_OFF, 0.0), (length + LABEL_OFF, 0.0)
        base, rot = BLOCK_BASE, (90.0 if vertical else 0.0)
    elif vertical:
        blk.add_line((0, 0), (0, length), dxfattribs={"layer": LAYER_STR_GRID})
        p_start, p_end = (0.0, -LABEL_OFF), (0.0, length + LABEL_OFF)
        base, rot = (0.0, 0.0), 0.0
    else:
        blk.add_line((0, 0), (length, 0), dxfattribs={"layer": LAYER_STR_GRID})
        p_start, p_end = (-LABEL_OFF, 0.0), (length + LABEL_OFF, 0.0)
        base, rot = (0.0, 0.0), 0.0
    pts = [p_end] + ([p_start] if dup else [])
    tags = []
    for i, p in enumerate(pts):
        if use_attrib:
            tag = f"GRIDNO{i}"
            blk.add_attdef(tag, insert=(p[0], p[1]), text=txt,
                           dxfattribs={"height": 250.0, "layer": "GRID-TEXT"})
            tags.append(tag)
        else:
            blk.add_text(txt, dxfattribs={"insert": (p[0], p[1], 0), "height": 250.0,
                                          "layer": "GRID-TEXT"})
    return name, base, rot, tags, txt


def _populate_str(doc, opts: SynthOptions):
    msp = doc.modelspace()
    vert, horiz = _grid_positions(opts, drawn=True)
    nvert, nhoriz = _grid_positions(opts, drawn=False)   # nominal (columns etc.)
    x0, x1, y0, y1 = _span(opts)

    for vertical, axes in ((True, vert), (False, horiz)):
        for lab, pos in axes:
            length = (y1 - y0) if vertical else (x1 - x0)
            start = (pos, y0) if vertical else (x0, pos)
            use_attr = opts.attrib_label is not None and lab == opts.attrib_label
            name, base, rot, tags, txt = _gridline_block(
                doc, opts, lab, 0, length, vertical, opts.rotated_grid_inserts, use_attr)
            rb = _rot(base, rot)
            ins = msp.add_blockref(name, (start[0] + rb[0], start[1] + rb[1]),
                                   dxfattribs={"rotation": rot, "layer": "GRID-TEXT"})
            if tags:
                ins.add_auto_attribs({t: txt for t in tags})

    if opts.decoy_line:
        vx = vert[0][1]
        msp.add_line((vx - 8000, y1 + LABEL_OFF - 200), (vx + 8000, y1 + LABEL_OFF - 200),
                     dxfattribs={"layer": LAYER_STR_GRID})

    # Shared column block, inserted at every nominal intersection.
    col = doc.blocks.new("Part-COL300")
    col.add_lwpolyline([(-150, -150), (150, -150), (150, 150), (-150, 150)], close=True,
                       dxfattribs={"layer": LAYER_COLUMN})
    col.add_line((-150, -150), (150, 150), dxfattribs={"layer": LAYER_COLUMN})
    col.add_line((-150, 150), (150, -150), dxfattribs={"layer": LAYER_COLUMN})
    h = col.add_hatch(color=3, dxfattribs={"layer": LAYER_COLUMN})
    h.paths.add_polyline_path([(-100, -100), (100, -100), (100, 100), (-100, 100)], is_closed=True)
    for (px, py) in _intersections(opts, nvert, nhoriz).values():
        msp.add_blockref("Part-COL300", (px, py), dxfattribs={"layer": LAYER_COLUMN})

    # Nested bay: a mirrored and a 45-degree rotated column + a line.
    bay = doc.blocks.new("Part-BAY")
    bay.add_blockref("Part-COL300", (1000, 0), dxfattribs={"xscale": -1.0, "layer": LAYER_COLUMN})
    bay.add_blockref("Part-COL300", (0, 1000), dxfattribs={"rotation": 45.0, "layer": LAYER_COLUMN})
    bay.add_line((0, 0), (1000, 400), dxfattribs={"layer": LAYER_BEAM})
    msp.add_blockref("Part-BAY", (3000, 2500), dxfattribs={"layer": LAYER_COLUMN})
    msp.add_blockref("Part-BAY", (9750, 7500), dxfattribs={"layer": LAYER_COLUMN})

    # TAG with different content than MECH's TAG.
    tag = doc.blocks.new("TAG")
    tag.add_circle((0, 0), 50.0, dxfattribs={"layer": LAYER_COLUMN})
    msp.add_blockref("TAG", (1000, 1000), dxfattribs={"layer": LAYER_COLUMN})

    # Gusset: 3-vertex closed polyline + SOLID.
    gus = doc.blocks.new("Part-GUSSET")
    gus.add_lwpolyline([(0, 0), (400, 0), (0, 400)], close=True, dxfattribs={"layer": LAYER_PLATE})
    gus.add_solid([(500, 0), (900, 0), (500, 400)], dxfattribs={"layer": LAYER_FILL})
    msp.add_blockref("Part-GUSSET", (3000, 1200), dxfattribs={"layer": LAYER_PLATE})

    # Beams and footing.
    hx = [p for _, p in nvert]
    hy = [p for _, p in nhoriz]
    for (_, y) in nhoriz:
        for xa, xb in zip(sorted(hx)[:-1], sorted(hx)[1:]):
            msp.add_line((xa, y + 400), (xb, y + 400), dxfattribs={"layer": LAYER_BEAM})
    fx, fy = sorted(hx)[1], sorted(hy)[1]
    msp.add_lwpolyline([(fx - 500, fy - 500), (fx + 500, fy - 500), (fx + 500, fy + 500),
                        (fx - 500, fy + 500)], close=True, dxfattribs={"layer": LAYER_FOOTING})

    # Anonymous block referencing Part-ANCHOR; *U1 referenced from modelspace.
    anc = doc.blocks.new("Part-ANCHOR")
    anc.add_circle((0, 0), 60.0, dxfattribs={"layer": LAYER_COLUMN})
    anc.add_line((-100, 0), (100, 0), dxfattribs={"layer": LAYER_COLUMN})
    u1 = doc.blocks.new("*U1")
    u1.add_blockref("Part-ANCHOR", (0, 0), dxfattribs={"layer": LAYER_COLUMN})
    u1.add_blockref("Part-ANCHOR", (200, 0), dxfattribs={"layer": LAYER_COLUMN})
    msp.add_blockref("*U1", (7000, 3000), dxfattribs={"layer": LAYER_COLUMN})

    # Never-referenced block (for orphan-removal tests).
    orph = doc.blocks.new("Part-ORPHAN")
    orph.add_circle((0, 0), 77.0, dxfattribs={"layer": LAYER_COLUMN})

    msp.add_text("ISMB 600", dxfattribs={"insert": (3000, 5400, 0), "height": 200.0,
                                         "layer": "STEEL-TEXT"})


# ---------------------------------------------------------------------------
# mismatch scene (step 8b.1)
# ---------------------------------------------------------------------------
#
# The scene is authored in frame P exactly like the base drawing, but it
# sits at ``scene_origin`` (30000, 0) — thousands of millimetres outside
# the grid bbox — so nothing in it can be mistaken for grid geometry.
#
# Two invariants the grid detector (PLAN.md step 4) depends on:
#   * every scene LINE is shorter than 0.15 x the drawing extent, so
#     ``merge_collinear`` filters it out before labels are associated;
#   * no scene block is named ``A$C*`` or ``RTR*`` — those are what the
#     MECH cleaner keeps (``keep_block_prefixes_mech``), so the detector
#     must be reading the *converted* file, not the cleaned one.
#
# STR blocks are named ``Part-*``, MECH blocks ``BOLT-*``/``SADDLE-*``/…
#
# Operations are tiny tuples so a case is pure data:
#   ("I", block, (x, y), layer, rotation_deg, xscale)   -> INSERT
#   ("L", (x, y), (x, y), layer)                        -> LINE
# Both are placed in frame P and transformed by build_pair afterwards.

B4 = ((-100.0, -100.0), (100.0, -100.0), (100.0, 100.0), (-100.0, 100.0))
B4_R10 = 10.0
HOLE_R11 = 11.0
PCD_R = 150.0
PLATE_HALF = 150.0            # Part-PLATE300* are 300 x 300
BAND_HALF_W = 100.0           # STR members are drawn as two lines at y = +/-100
BAND_HALF_LEN = 2500.0        # ... spanning x = +/-2500


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _poly(x0, y0, x1, y1):
    return [(x0, y0, 0.0), (x1, y0, 0.0), (x1, y1, 0.0), (x0, y1, 0.0)]


def _cell(opts: SynthOptions, c, r):
    """Origin of scene cell ``(column, row)`` in frame P."""
    return (opts.scene_origin[0] + opts.scene_pitch * c,
            opts.scene_origin[1] + opts.scene_pitch * r)


def _to_mech(opts: SynthOptions, p):
    """Frame P -> MECH world (the MECH file is only ever translated)."""
    return (p[0] + opts.mech_offset[0], p[1] + opts.mech_offset[1])


def _pv(opts: SynthOptions, case: str, key: str, default):
    """Read a ``mismatch_params`` override for ``case``."""
    return opts.mismatch_params.get(case, {}).get(key, default)


@dataclass(frozen=True)
class _SceneCase:
    name: str
    mech_ops: tuple
    str_ops: tuple
    expected: tuple = ()
    controls: tuple = ()


# -- MECH block definitions ---------------------------------------------------

def _define_mech_scene_blocks(doc: SynthOptions | None = None) -> None:
    """Every MECH scene block, defined exactly once per document."""
    opts = doc or opts_default
    if doc is not None and doc.blocks.get("BOLT-M20-4") is not None:
        return
    d = _mech_doc
    b = d.blocks.new("BOLT-M20-4")
    for p in B4:
        b.add_circle(p, B4_R10, dxfattribs={"layer": LAYER_BOLT})

    # The one case whose defect lives *inside* the block, so the block name
    # encodes the parameters it was built with.
    mag = float(_pv(opts, "bolt_offset", "bolt_offset_mm", 6.0))
    ang = float(_pv(opts, "bolt_offset", "bolt_offset_deg", 35.0))
    dx, dy = mag * math.cos(math.radians(ang)), mag * math.sin(math.radians(ang))
    name = f"BOLT-M20-4-X{mag:.6g}-{ang:.6g}"
    b = d.blocks.new(name)
    for p in B4:
        q = (p[0] + dx, p[1] + dy) if p == (100.0, 100.0) else p
        b.add_circle(q, B4_R10, dxfattribs={"layer": LAYER_BOLT})

    b = d.blocks.new("BOLT-ASYM")
    for p in _ASYM:
        b.add_circle(p, B4_R10, dxfattribs={"layer": LAYER_BOLT})

    b = d.blocks.new("BASEPLATE-400")
    b.add_lwpolyline(_poly(-200, -200, 200, 200), format="xy", close=True,
                     dxfattribs={"layer": LAYER_BASEPLATE_ENV})
    for p in B4:
        b.add_circle((p[0] * 1.5, p[1] * 1.5), 15.0, dxfattribs={"layer": LAYER_BASEPLATE})

    b = d.blocks.new("FLANGE-8B")
    for k in range(8):
        a = math.radians(45.0 * k)
        b.add_circle((PCD_R * math.cos(a), PCD_R * math.sin(a)), 8.0,
                     dxfattribs={"layer": LAYER_BOLT})

    b = d.blocks.new("SADDLE-300x150")
    b.add_lwpolyline(_poly(-150, -75, 150, 75), format="xy", close=True,
                     dxfattribs={"layer": LAYER_SADDLE})
    b = d.blocks.new("SADDLE-400x250")
    b.add_lwpolyline(_poly(-200, -125, 200, 125), format="xy", close=True,
                     dxfattribs={"layer": LAYER_SADDLE})

    # circle + X-cross on layer "0" -> both inherit PIPE-SUPPORT from the
    # INSERT.  The X (not a +) keeps the two lines from ever being
    # reconstructed into a rect, which would otherwise make the support
    # point a mech_support as well.
    b = d.blocks.new("PS-01")
    b.add_circle((0, 0), 50.0, dxfattribs={"layer": "0"})
    b.add_line((-70, -70), (70, 70), dxfattribs={"layer": "0"})
    b.add_line((-70, 70), (70, -70), dxfattribs={"layer": "0"})

    b = d.blocks.new("SKID-1800x1000")
    b.add_lwpolyline(_poly(-900, -500, 900, 500), format="xy", close=True,
                     dxfattribs={"layer": LAYER_SKID})
    b = d.blocks.new("EQUIP-1000")
    b.add_lwpolyline(_poly(-500, -500, 500, 500), format="xy", close=True,
                     dxfattribs={"layer": LAYER_EQUIP})


#: 5-bolt asymmetric pattern used by the mirrored/rotated control case.
_ASYM = ((-150.0, -75.0), (150.0, -75.0), (150.0, 75.0), (-150.0, 75.0), (0.0, 75.0))


def _define_str_scene_blocks(doc) -> None:
    """Every STR scene block, defined exactly once per document."""
    if doc.blocks.get("Part-HOLE-R11") is not None:
        return
    hole = doc.blocks.new("Part-HOLE-R11")
    hole.add_circle((0, 0), HOLE_R11, dxfattribs={"layer": "0"})

    def plate(name, points):
        blk = doc.blocks.new(name)
        blk.add_lwpolyline(_poly(-PLATE_HALF, -PLATE_HALF, PLATE_HALF, PLATE_HALF),
                           format="xy", close=True, dxfattribs={"layer": LAYER_PLATE})
        for p in points:
            blk.add_blockref("Part-HOLE-R11", p, dxfattribs={"layer": LAYER_BOLT_HOLE})
        return blk

    plate("Part-PLATE300-H4", B4)
    plate("Part-PLATE300-H3", tuple(p for p in B4 if p != (-100.0, -100.0)))
    doc.blocks.new("Part-PLATE300").add_lwpolyline(
        _poly(-PLATE_HALF, -PLATE_HALF, PLATE_HALF, PLATE_HALF), format="xy", close=True,
        dxfattribs={"layer": LAYER_PLATE})

    # Mirrored twice: the block content is pre-mirrored in x and the INSERT
    # carries xscale=-1, so the net composition is exactly the MECH side's
    # +30 deg rotation.  (Two mirrors cancel.)
    asym = doc.blocks.new("Part-PLATE-ASYM")
    for p in _ASYM:
        q = _rot(p, 30.0)
        asym.add_blockref("Part-HOLE-R11", (-q[0], q[1]), dxfattribs={"layer": LAYER_BOLT_HOLE})

    anc = doc.blocks.new("Part-ANCHOR4")
    for p in B4:
        anc.add_circle((p[0] * 1.5, p[1] * 1.5), 12.0, dxfattribs={"layer": LAYER_ANCHOR})

    flg = doc.blocks.new("Part-FLGPLATE-8H")
    for k in range(8):
        a = math.radians(45.0 * k)
        flg.add_circle((PCD_R * math.cos(a), PCD_R * math.sin(a)), 9.0,
                       dxfattribs={"layer": LAYER_PLATE})

    doc.blocks.new("Part-BEAM-W200").add_lwpolyline(
        _poly(-5000.0, -100.0, 5000.0, 100.0), format="xy", close=True,
        dxfattribs={"layer": LAYER_BEAM})
    doc.blocks.new("Part-PLINT2000x1200").add_lwpolyline(
        _poly(-1000.0, -600.0, 1000.0, 600.0), format="xy", close=True,
        dxfattribs={"layer": LAYER_FOOTING})


# -- the 18 cases -------------------------------------------------------------
# Each builder returns draws (in frame P) plus the analytically derived
# expected findings / control regions.  Nothing here consults ezdxf.

def _ins(block, at, layer, rot=0.0, xscale=1.0):
    return ("I", block, at, layer, rot, xscale)


def _band_ops(O):
    """The two FRAME-STEEL lines that become one 200 mm band at y = 0."""
    return (("L", (O[0] - BAND_HALF_LEN, O[1] - BAND_HALF_W), (O[0] + BAND_HALF_LEN, O[1] - BAND_HALF_W), LAYER_BEAM),
            ("L", (O[0] - BAND_HALF_LEN, O[1] + BAND_HALF_W), (O[0] + BAND_HALF_LEN, O[1] + BAND_HALF_W), LAYER_BEAM))


def _case_ctl_bp4(opts):
    O = _cell(opts, 0, 0)
    return _SceneCase(
        "ctl_bp4",
        (_ins("BOLT-M20-4", O, LAYER_BOLT),),
        (_ins("Part-PLATE300-H4", O, LAYER_PLATE),),
        controls=(ControlRegion("ctl_bp4", _to_mech(opts, O), 400.0),))


def _case_ctl_bp_rot(opts):
    O = _cell(opts, 1, 0)
    return _SceneCase(
        "ctl_bp_rot",
        (_ins("BOLT-ASYM", O, LAYER_BOLT, 30.0),),
        (_ins("Part-PLATE-ASYM", O, LAYER_PLATE, 0.0, -1.0),),
        controls=(ControlRegion("ctl_bp_rot", _to_mech(opts, O), 500.0),))


def _case_bolt_offset(opts):
    mag = float(_pv(opts, "bolt_offset", "bolt_offset_mm", 6.0))
    ang = float(_pv(opts, "bolt_offset", "bolt_offset_deg", 35.0))
    O = _cell(opts, 2, 0)
    dx, dy = mag * math.cos(math.radians(ang)), mag * math.sin(math.radians(ang))
    bolt = (O[0] + 100.0 + dx, O[1] + 100.0 + dy)
    return _SceneCase(
        "bolt_offset",
        (_ins(f"BOLT-M20-4-X{mag:.6g}-{ang:.6g}", O, LAYER_BOLT),),
        (_ins("Part-PLATE300-H4", O, LAYER_PLATE),),
        expected=(ExpectedFinding(
            "bolt_offset", "BOLT_HOLE_OFFSET", "CRITICAL",
            location_mech=_to_mech(opts, bolt), magnitude_mm=mag,
            offset_mm=(-dx, -dy)),))


def _case_pattern_offset(opts):
    mag = float(_pv(opts, "pattern_offset", "pattern_offset_mm", 8.0))
    ang = float(_pv(opts, "pattern_offset", "pattern_offset_deg", 120.0))
    O = _cell(opts, 3, 0)
    dx, dy = mag * math.cos(math.radians(ang)), mag * math.sin(math.radians(ang))
    return _SceneCase(
        "pattern_offset",
        (_ins("BOLT-M20-4", (O[0] + dx, O[1] + dy), LAYER_BOLT),),
        (_ins("Part-PLATE300-H4", O, LAYER_PLATE),),
        expected=(ExpectedFinding(
            "pattern_offset", "PATTERN_OFFSET", "CRITICAL",
            location_mech=_to_mech(opts, (O[0] + dx, O[1] + dy)), magnitude_mm=mag,
            offset_mm=(-dx, -dy)),))


def _case_anchor_rotated(opts):
    rot = float(_pv(opts, "anchor_rotated", "anchor_rot_deg", 4.0))
    O = _cell(opts, 4, 0)
    r_max = PCD_R * math.sqrt(2.0)                 # 212.132 mm to a corner
    mag = 2.0 * r_max * math.sin(math.radians(rot) / 2.0)
    return _SceneCase(
        "anchor_rotated",
        (_ins("BASEPLATE-400", O, LAYER_BASEPLATE),),
        (_ins("Part-ANCHOR4", O, LAYER_ANCHOR, rot),),
        expected=(ExpectedFinding(
            "anchor_rotated", "PATTERN_ROTATED", "CRITICAL",
            location_mech=_to_mech(opts, O), magnitude_mm=mag,
            offset_mm=(0.0, 0.0), rotation_deg=rot),))


def _case_flange_pcd_offset(opts):
    mag = float(_pv(opts, "flange_pcd_offset", "pcd_offset_mm", 5.0))
    O = _cell(opts, 5, 0)
    mech_at = (O[0], O[1] + mag)
    return _SceneCase(
        "flange_pcd_offset",
        (_ins("FLANGE-8B", mech_at, LAYER_BOLT),),
        (_ins("Part-FLGPLATE-8H", O, LAYER_PLATE),),
        expected=(ExpectedFinding(
            "flange_pcd_offset", "PATTERN_OFFSET", "CRITICAL",
            location_mech=_to_mech(opts, mech_at), magnitude_mm=mag,
            offset_mm=(0.0, -mag)),))


def _case_missing_hole(opts):
    O = _cell(opts, 0, 1)
    bolt = (O[0] - 100.0, O[1] - 100.0)
    return _SceneCase(
        "missing_hole",
        (_ins("BOLT-M20-4", O, LAYER_BOLT),),
        (_ins("Part-PLATE300-H3", O, LAYER_PLATE),),
        expected=(ExpectedFinding(
            "missing_hole", "MISSING_HOLE", "CRITICAL",
            location_mech=_to_mech(opts, bolt)),))


def _case_bolt_off_plate(opts):
    shift = float(_pv(opts, "bolt_off_plate", "plate_shift_mm", 90.0))
    O = _cell(opts, 1, 1)
    mech_at = (O[0] + shift, O[1])
    x_out = mech_at[0] + 100.0                    # the bolt that clears the plate edge
    outside = PLATE_HALF + shift - 100.0          # distance past the plate edge
    return _SceneCase(
        "bolt_off_plate",
        (_ins("BOLT-M20-4", mech_at, LAYER_BOLT),),
        (_ins("Part-PLATE300", O, LAYER_PLATE),),
        expected=(
            ExpectedFinding("bolt_off_plate", "BOLT_OFF_PLATE", "CRITICAL",
                            location_mech=_to_mech(opts, (x_out, O[1] + 100.0)),
                            magnitude_mm=outside, offset_mm=(outside, 0.0)),
            ExpectedFinding("bolt_off_plate", "BOLT_OFF_PLATE", "CRITICAL",
                            location_mech=_to_mech(opts, (x_out, O[1] - 100.0)),
                            magnitude_mm=outside, offset_mm=(outside, 0.0)),
        ))


def _case_saddle_off_beam(opts):
    shift = float(_pv(opts, "saddle_off_beam", "saddle_shift_mm", 120.0))
    O = _cell(opts, 2, 1)
    at = (O[0], O[1] + shift)
    # saddle 300 (x) x 150 (y) centred at y = +120; band centreline y = 0, hw = 100
    bearing = (BAND_HALF_W - (shift - 75.0)) / 150.0
    return _SceneCase(
        "saddle_off_beam",
        (_ins("SADDLE-300x150", at, LAYER_SADDLE),),
        _band_ops(O),
        expected=(ExpectedFinding(
            "saddle_off_beam", "SUPPORT_OFF_MEMBER", "CRITICAL",
            location_mech=_to_mech(opts, at), magnitude_mm=shift,
            offset_mm=(0.0, -shift), details={"bearing_ratio": bearing}),))


def _case_ctl_saddle(opts):
    shift = float(_pv(opts, "ctl_saddle", "base_shift_mm", 20.0))
    along = float(_pv(opts, "ctl_saddle", "base_along_mm", 500.0))
    O = _cell(opts, 3, 1)
    at2 = (O[0] + shift, O[1] + along)
    return _SceneCase(
        "ctl_saddle",
        (_ins("SADDLE-300x150", O, LAYER_SADDLE), _ins("SADDLE-300x150", at2, LAYER_SADDLE)),
        (_ins("Part-BEAM-W200", O, LAYER_BEAM, 90.0),),
        controls=(ControlRegion("ctl_saddle", _to_mech(opts, O), 400.0),
                  ControlRegion("ctl_saddle", _to_mech(opts, at2), 400.0)))


def _case_pipe_support_off_flange(opts):
    shift = float(_pv(opts, "pipe_support_off_flange", "ps_offset_mm", 160.0))
    O = _cell(opts, 4, 1)
    at = (O[0], O[1] + shift)
    return _SceneCase(
        "pipe_support_off_flange",
        (_ins("PS-01", at, LAYER_PIPE_SUP),),
        _band_ops(O),
        expected=(ExpectedFinding(
            "pipe_support_off_flange", "SUPPORT_POINT_OFF_MEMBER", "CRITICAL",
            location_mech=_to_mech(opts, at), magnitude_mm=shift,
            offset_mm=(0.0, -shift),
            details={"outside_by_mm": shift - BAND_HALF_W}),))


def _case_ctl_pipe_support(opts):
    d1 = float(_pv(opts, "ctl_pipe_support", "ps1_x_mm", -1000.0))
    d2 = float(_pv(opts, "ctl_pipe_support", "ps2_x_mm", 1000.0))
    dy2 = float(_pv(opts, "ctl_pipe_support", "ps2_y_mm", 40.0))
    O = _cell(opts, 5, 1)
    a = (O[0] + d1, O[1])
    b = (O[0] + d2, O[1] + dy2)
    return _SceneCase(
        "ctl_pipe_support",
        (_ins("PS-01", a, LAYER_PIPE_SUP), _ins("PS-01", b, LAYER_PIPE_SUP)),
        _band_ops(O),
        controls=(ControlRegion("ctl_pipe_support", _to_mech(opts, a), 400.0),
                  ControlRegion("ctl_pipe_support", _to_mech(opts, b), 400.0)))


def _case_missing_counterpart(opts):
    O = _cell(opts, 0, 2)
    return _SceneCase(
        "missing_counterpart",
        (_ins("SADDLE-400x250", O, LAYER_SADDLE),),
        (),
        expected=(ExpectedFinding(
            "missing_counterpart", "MISSING_COUNTERPART", "CRITICAL",
            location_mech=_to_mech(opts, O)),))


def _outside_case(opts, name, c, shift, control):
    O = _cell(opts, c, 2)
    at = (O[0] + shift, O[1])
    # skid 1800 x 1000 on a 2000 x 1200 plinth: worst vertex is (900+shift, +-500)
    worst_x = 900.0 + shift
    outside = worst_x - 1000.0
    ops_m = (_ins("SKID-1800x1000", at, LAYER_SKID),)
    ops_s = (_ins("Part-PLINT2000x1200", O, LAYER_FOOTING),)
    if control:
        return _SceneCase(name, ops_m, ops_s,
                          controls=(ControlRegion(name, _to_mech(opts, at), 600.0),))
    if outside > 0.0:
        exp = (ExpectedFinding(name, "OUTSIDE_OUTLINE", "CRITICAL",
                               location_mech=_to_mech(opts, (worst_x, 500.0)),
                               magnitude_mm=outside, offset_mm=(-outside, 0.0)),)
    else:
        edge = 1000.0 - worst_x                     # 70 -> 30 mm, 0 -> 100 mm
        edge = min(edge, 600.0 - 500.0)
        if edge < 50.0:
            exp = (ExpectedFinding(name, "EDGE_DISTANCE", "WARNING",
                                   location_mech=_to_mech(opts, (worst_x, -500.0)),
                                   magnitude_mm=50.0 - edge,
                                   offset_mm=(-(50.0 - edge), 0.0),
                                   details={"edge_distance_mm": edge}),)
        else:
            exp = ()
    return _SceneCase(name, ops_m, ops_s, expected=exp)


def _case_outside_outline(opts):
    return _outside_case(opts, "outside_outline", 1,
                         float(_pv(opts, "outside_outline", "skid_shift_mm", 250.0)),
                         control=False)


def _case_edge_distance(opts):
    return _outside_case(opts, "edge_distance", 2,
                         float(_pv(opts, "edge_distance", "skid_shift_mm", 70.0)),
                         control=False)


def _case_ctl_outline(opts):
    return _outside_case(opts, "ctl_outline", 3,
                         float(_pv(opts, "ctl_outline", "skid_shift_mm", 0.0)),
                         control=True)


def _clash_case(opts, name, c, shift, control):
    O = _cell(opts, c, 2)
    at = (O[0] + shift, O[1])
    overlap = 150.0 - (shift + 500.0) + 150.0     # column x=-150..150 vs rect
    ops = (_ins("EQUIP-1000", at, LAYER_EQUIP),)
    ops_s = (_ins("Part-COL300", O, LAYER_COLUMN),)
    if control or overlap <= 1.0:
        return _SceneCase(name, ops, ops_s,
                          controls=(ControlRegion(name, _to_mech(opts, at), 800.0),))
    return _SceneCase(name, ops, ops_s, expected=(
        ExpectedFinding(name, "CLASH", "CRITICAL",
                        location_mech=_to_mech(opts, at), magnitude_mm=overlap,
                        offset_mm=(-overlap, 0.0)),))


def _case_clash(opts):
    return _clash_case(opts, "clash", 4,
                       float(_pv(opts, "clash", "equip_shift_mm", -570.0)), control=False)


def _case_ctl_clash(opts):
    return _clash_case(opts, "ctl_clash", 5,
                       float(_pv(opts, "ctl_clash", "equip_shift_mm", -850.0)), control=True)


def _case_pattern_array(opts):
    """A 4x4 grid of ctl_bp4 at ``array_pitch``; element (2, 1) gets +8 mm."""
    n = int(_pv(opts, "pattern_array", "n", 4))
    pitch = float(_pv(opts, "pattern_array", "array_pitch_mm", 1500.0))
    extra = float(_pv(opts, "pattern_array", "defect_shift_mm", 8.0))
    dcell = _pv(opts, "pattern_array", "defect_cell", (2, 1))
    gx, gy = opts.mismatch_global_shift_mm
    mech_ops, str_ops, expected = [], [], []
    for c in range(n):
        for r in range(n):
            O = (opts.scene_origin[0] + pitch * c, opts.scene_origin[1] + pitch * r)
            mech_at = (O[0] + gx, O[1] + gy)
            if (c, r) == tuple(dcell):
                mech_at = (mech_at[0], mech_at[1] + extra)
            mech_ops.append(_ins("BOLT-M20-4", mech_at, LAYER_BOLT))
            str_ops.append(_ins("Part-PLATE300-H4", O, LAYER_PLATE))
            off = (O[0] - mech_at[0], O[1] - mech_at[1])
            mag = math.hypot(*off)
            if mag > 1e-9:
                expected.append(ExpectedFinding(
                    "pattern_array", "PATTERN_OFFSET", "CRITICAL",
                    location_mech=_to_mech(opts, mech_at),
                    magnitude_mm=mag, offset_mm=off))
    return _SceneCase("pattern_array", tuple(mech_ops), tuple(str_ops),
                      expected=tuple(expected))


_CASE_BUILDERS = {
    "ctl_bp4": _case_ctl_bp4,
    "ctl_bp_rot": _case_ctl_bp_rot,
    "bolt_offset": _case_bolt_offset,
    "pattern_offset": _case_pattern_offset,
    "anchor_rotated": _case_anchor_rotated,
    "flange_pcd_offset": _case_flange_pcd_offset,
    "missing_hole": _case_missing_hole,
    "bolt_off_plate": _case_bolt_off_plate,
    "saddle_off_beam": _case_saddle_off_beam,
    "ctl_saddle": _case_ctl_saddle,
    "pipe_support_off_flange": _case_pipe_support_off_flange,
    "ctl_pipe_support": _case_ctl_pipe_support,
    "missing_counterpart": _case_missing_counterpart,
    "outside_outline": _case_outside_outline,
    "edge_distance": _case_edge_distance,
    "ctl_outline": _case_ctl_outline,
    "clash": _case_clash,
    "ctl_clash": _case_ctl_clash,
    "pattern_array": _case_pattern_array,
}

#: Every named case, in the §6.1 table order.
ALL_MISMATCH_CASES = (
    "ctl_bp4", "ctl_bp_rot", "bolt_offset", "pattern_offset",
    "anchor_rotated", "flange_pcd_offset", "missing_hole", "bolt_off_plate",
    "saddle_off_beam", "ctl_saddle", "pipe_support_off_flange",
    "ctl_pipe_support", "missing_counterpart", "outside_outline",
    "edge_distance", "ctl_outline", "clash", "ctl_clash",
)


def scene_cases(opts: SynthOptions) -> tuple[_SceneCase, ...]:
    """Build (and validate) the requested mismatch scene cases."""
    unknown = [c for c in opts.mismatch_cases if c not in _CASE_BUILDERS]
    if unknown:
        raise ValueError(f"unknown mismatch case(s): {unknown}; "
                         f"known: {sorted(_CASE_BUILDERS)}")
    cases = [_CASE_BUILDERS[name](opts) for name in opts.mismatch_cases]
    if opts.scene_reverse_order:
        cases = list(reversed(cases))
        cases = [_SceneCase(c.name, tuple(reversed(c.mech_ops)), tuple(reversed(c.str_ops)),
                            c.expected, c.controls) for c in cases]
    return tuple(cases)


def _draw_ops(doc, ops) -> None:
    msp = doc.modelspace()
    for op in ops:
        if op[0] == "I":
            _, block, at, layer, rot, xscale = op
            attrs = {"layer": layer}
            if rot:
                attrs["rotation"] = rot
            if xscale != 1.0:
                attrs["xscale"] = xscale
            msp.add_blockref(block, at, dxfattribs=attrs)
        elif op[0] == "L":
            _, a, b, layer = op
            msp.add_line(a, b, dxfattribs={"layer": layer})
        else:                                       # pragma: no cover - internal
            raise ValueError(f"unknown scene op {op[0]!r}")


def _shift_mech_ops(ops, shift):
    """Apply ``mismatch_global_shift_mm`` to MECH scene draws, in frame P."""
    sx, sy = float(shift[0]), float(shift[1])
    if not sx and not sy:
        return tuple(ops)
    out = []
    for op in ops:
        if op[0] == "I":
            at = (op[2][0] + sx, op[2][1] + sy)
            out.append((op[0], op[1], at, *op[3:]))
        else:
            out.append((op[0], (op[1][0] + sx, op[1][1] + sy),
                        (op[2][0] + sx, op[2][1] + sy), op[3]))
    return tuple(out)


def mismatch_truth(cases) -> tuple[tuple[ExpectedFinding, ...], tuple[ControlRegion, ...]]:
    """``(expected, controls)`` for a set of built cases."""
    exp, ctl = [], []
    for c in cases:
        exp += list(c.expected)
        ctl += list(c.controls)
    return tuple(exp), tuple(ctl)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def truth_for(opts: SynthOptions) -> GroundTruth:
    u = 1.0 if opts.str_units == "mm" else 0.001
    return GroundTruth(
        scale=1.0 / u,
        rotation_deg=opts.str_rotation_deg,
        tx=opts.mech_offset[0] - opts.str_offset[0],
        ty=opts.mech_offset[1] - opts.str_offset[1],
    )


def _str_map(opts: SynthOptions):
    """Analytic frame-P -> STR file map, p_str = u * R(-theta) (p + t_s)."""
    u = 1.0 if opts.str_units == "mm" else 0.001

    def f(p):
        q = _rot((p[0] + opts.str_offset[0], p[1] + opts.str_offset[1]), -opts.str_rotation_deg)
        return (u * q[0], u * q[1])
    return f


def build_pair(out_dir: Path, opts: SynthOptions | None = None) -> SynthPair:
    opts = opts or SynthOptions()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    u = 1.0 if opts.str_units == "mm" else 0.001
    truth = truth_for(opts)

    # MECH
    mdoc = _new_doc(4)
    _populate_mech(mdoc, opts)
    _ensure_layers(mdoc)
    B = Matrix44.translate(opts.mech_offset[0], opts.mech_offset[1], 0.0)
    ez_transform.inplace(list(mdoc.modelspace()), B)
    mech_path = out_dir / opts.mech_name
    mdoc.saveas(mech_path)

    # STR
    sdoc = _new_doc(4 if opts.str_units == "mm" else 6)
    _populate_str(sdoc, opts)
    _ensure_layers(sdoc)
    A = Matrix44.chain(
        Matrix44.translate(opts.str_offset[0], opts.str_offset[1], 0.0),
        Matrix44.z_rotate(math.radians(-opts.str_rotation_deg)),
        Matrix44.scale(u, u, u),
    )
    ez_transform.inplace(list(sdoc.modelspace()), A)
    if opts.stray_str_entity_at is not None:
        sx, sy = opts.stray_str_entity_at
        sdoc.modelspace().add_circle((sx, sy), 1.0 * (1.0 if u == 1.0 else u),
                                     dxfattribs={"layer": "STRAY"})
        _ensure_layers(sdoc)
    str_path = out_dir / opts.str_name
    sdoc.saveas(str_path)

    # Analytic ground-truth bookkeeping.
    vert_n, horiz_n = _grid_positions(opts, drawn=False)
    vert_d, horiz_d = _grid_positions(opts, drawn=True)
    off = opts.mech_offset
    to_str = _str_map(opts)

    grid_p, grid_mech, grid_str = {}, {}, {}
    x0, x1, y0, y1 = _span(opts)
    for vertical, nom, drawn in ((True, vert_n, vert_d), (False, horiz_n, horiz_d)):
        d = (0.0, 1.0) if vertical else (1.0, 0.0)
        for (lab, pn), (_, pd) in zip(nom, drawn):
            pt_n = (pn, y0) if vertical else (x0, pn)
            pt_d = (pd, y0) if vertical else (x0, pd)
            grid_p[lab] = (Vec2(pt_n), Vec2(d))
            grid_mech[lab] = (Vec2(pt_n[0] + off[0], pt_n[1] + off[1]), Vec2(d))
            ps = to_str(pt_d)
            dd = _rot(d, -opts.str_rotation_deg)
            grid_str[lab] = (Vec2(ps), Vec2(dd))

    inter_p_nom = _intersections(opts, vert_n, horiz_n)
    inter_p_drawn = _intersections(opts, vert_d, horiz_d)
    inter_mech = {k: Vec2(v[0] + off[0], v[1] + off[1]) for k, v in inter_p_nom.items()}
    inter_str = {k: Vec2(to_str(v)) for k, v in inter_p_drawn.items()}

    return SynthPair(mech_path=mech_path, str_path=str_path, truth=truth, opts=opts,
                     grid_p=grid_p, intersections_mech=inter_mech,
                     intersections_str=inter_str, grid_mech=grid_mech, grid_str=grid_str)


def with_opts(base: SynthOptions, **kw) -> SynthOptions:
    """dataclasses.replace shortcut (copies mutable fields)."""
    kw.setdefault("shift_str_axis", dict(base.shift_str_axis))
    return replace(base, **kw)
