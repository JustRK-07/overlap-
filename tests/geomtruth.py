#!/usr/bin/env python3
"""geomtruth.py — world-space geometry comparison harness for synthetic tests.

``world_primitives`` flattens a modelspace into comparable primitives (all
INSERTs recursively decomposed), ``map_prims`` applies a ground-truth
similarity, ``assert_prims_match`` compares two primitive lists with a
symmetric point-to-polyline Hausdorff tolerance.

Tolerance rule: ``tol = 2 * FLAT + 1e-9 * extent`` where FLAT is the
flattening distance in the *comparison frame*.  ``flat_for(doc)`` returns
the flatten distance to use for a document so that it equals 1e-3 mm
regardless of $INSUNITS (mm: 1e-3, m: 1e-6, ...); ``tolerance`` builds tol
for the comparison frame (mm by default).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Iterable

import numpy as np
from ezdxf import disassemble, path as ez_path
from ezdxf.units import conversion_factor

FLAT_MM = 1e-3  # flatten distance in millimetres


@dataclass
class Prim:
    kind: str                      # "CURVE" | "TEXT" | "POINT"
    layer: str
    pts: np.ndarray                # (N, 2)
    text: str | None = None
    height: float | None = None    # TEXT only
    rotation: float | None = None  # TEXT only, degrees


def flat_for(doc) -> float:
    """Flattening distance in this doc's units equal to FLAT_MM."""
    code = int(doc.header.get("$INSUNITS", 0) or 0)
    if code == 0:
        code = 4
    try:
        per_unit_mm = conversion_factor(code, 4)  # 1 doc unit expressed in mm
    except Exception:
        per_unit_mm = 1.0
    return FLAT_MM / per_unit_mm


def tolerance(extent: float, flat: float = FLAT_MM) -> float:
    return 2.0 * flat + 1e-9 * extent


def _text_height_rot(e):
    t = e.dxftype()
    if t == "MTEXT":
        h = float(e.dxf.get("char_height", 0.0))
        d = e.dxf.get("text_direction", None)
        rot = math.degrees(math.atan2(d[1], d[0])) if d is not None else float(e.dxf.get("rotation", 0.0))
        return h, rot
    return float(e.dxf.get("height", 0.0)), float(e.dxf.get("rotation", 0.0))


def _text_prim(e) -> Prim:
    t = e.dxftype()
    if t == "MTEXT":
        text = e.plain_text()
    else:
        text = e.dxf.text
    ins = e.dxf.insert
    h, rot = _text_height_rot(e)
    return Prim("TEXT", e.dxf.layer, np.array([[ins[0], ins[1]]], dtype=float), text, h, rot % 360.0)


def _path_prims(p, layer, flat) -> Iterable[Prim]:
    for sp in p.sub_paths():
        pts = np.array([[v.x, v.y] for v in sp.flattening(flat)], dtype=float)
        if len(pts):
            yield Prim("CURVE", layer, pts)


def world_primitives(doc, *, layer_pred: Callable[[str], bool] = lambda l: True,
                     flat: float | None = None) -> list[Prim]:
    """All modelspace content in world coordinates.

    layer_pred is applied to the layer of each decomposed (virtual) entity.
    ATTRIBs of INSERTs are included as TEXT (recursive_decompose yields them).
    """
    flat = flat_for(doc) if flat is None else flat
    msp = doc.modelspace()
    prims: list[Prim] = []

    def handle(e):
        if not layer_pred(e.dxf.layer):
            return
        t = e.dxftype()
        if t in ("TEXT", "MTEXT", "ATTRIB"):
            prims.append(_text_prim(e))
        elif t == "POINT":
            loc = e.dxf.location
            prims.append(Prim("POINT", e.dxf.layer, np.array([[loc[0], loc[1]]], dtype=float)))
        elif t == "ATTDEF":
            return
        elif t == "HATCH":
            for p in ez_path.from_hatch(e):
                prims.extend(_path_prims(p, e.dxf.layer, flat))
        elif t in ("CIRCLE", "ARC", "ELLIPSE", "SPLINE"):
            # Entity-native flattening: path.make_path() approximates these
            # with Beziers whose error (~2e-4 * r) depends on the start
            # angle, so it would differ between a rotated entity and the
            # same entity re-created in the rotated frame.
            pts = np.array([[v.x, v.y] for v in e.flattening(flat)], dtype=float)
            prims.append(Prim("CURVE", e.dxf.layer, pts))
        else:
            prims.extend(_path_prims(ez_path.make_path(e), e.dxf.layer, flat))

    for e in disassemble.recursive_decompose(msp):
        handle(e)
    return prims


def map_prims(prims: list[Prim], truth) -> list[Prim]:
    """Apply the ground-truth similarity (STR -> MECH) to every primitive."""
    out = []
    for p in prims:
        pts = np.asarray(truth.apply(p.pts), dtype=float)
        if p.kind == "TEXT":
            out.append(Prim("TEXT", p.layer, pts, p.text,
                            None if p.height is None else p.height * truth.scale,
                            None if p.rotation is None else (p.rotation + truth.rotation_deg) % 360.0))
        else:
            out.append(Prim(p.kind, p.layer, pts))
    return out


def _seg_dist(points: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Distance of each point to a polyline (or a single point)."""
    if len(poly) == 1:
        return np.hypot(*(points - poly[0]).T)
    a = poly[:-1][None, :, :]
    b = poly[1:][None, :, :]
    pt = points[:, None, :]
    ab = b - a
    denom = np.maximum((ab ** 2).sum(-1), 1e-300)
    t = np.clip(((pt - a) * ab).sum(-1) / denom, 0.0, 1.0)
    proj = a + t[..., None] * ab
    return np.sqrt(((pt - proj) ** 2).sum(-1)).min(axis=1)


def hausdorff(a: np.ndarray, b: np.ndarray) -> float:
    return float(max(_seg_dist(a, b).max(), _seg_dist(b, a).max()))


def _bbox_center(p: Prim):
    return (p.pts.min(axis=0) + p.pts.max(axis=0)) / 2.0


def _describe(p: Prim) -> str:
    c = _bbox_center(p)
    return f"{p.kind}[{p.layer}] n={len(p.pts)} c=({c[0]:.6g},{c[1]:.6g}) text={p.text!r}"


def assert_prims_match(actual: list[Prim], expected: list[Prim], tol: float) -> None:
    """Bucket by kind, pair greedily, require Hausdorff <= tol and equal counts."""
    for kind in ("CURVE", "TEXT", "POINT"):
        act = [p for p in actual if p.kind == kind]
        exp = [p for p in expected if p.kind == kind]
        assert len(act) == len(exp), (
            f"{kind}: count mismatch actual={len(act)} expected={len(exp)}")
        if not exp:
            continue
        a_centers = np.array([_bbox_center(p) for p in act])
        unmatched = set(range(len(act)))
        for e in exp:
            ec = _bbox_center(e)
            dist = np.hypot(*(a_centers - ec).T)
            cand = [i for i in unmatched if dist[i] <= 2.0 * tol + 1e-12]
            best, best_h = None, math.inf
            for i in cand:
                a = act[i]
                if kind == "TEXT" and a.text != e.text:
                    continue
                h = hausdorff(a.pts, e.pts)
                if h < best_h:
                    best, best_h = i, h
            assert best is not None and best_h <= tol, (
                f"{kind}: no match for expected {_describe(e)} "
                f"(best Hausdorff {best_h:.3g} > tol {tol:.3g}; candidates={len(cand)})")
            unmatched.discard(best)
            if kind == "TEXT":
                a = act[best]
                if e.height is not None and a.height is not None:
                    assert abs(a.height - e.height) <= 1e-9 * max(1.0, abs(e.height)) + tol, (
                        f"TEXT height {a.height} != {e.height} for {e.text!r}")
                if e.rotation is not None and a.rotation is not None:
                    d = abs((a.rotation - e.rotation + 180.0) % 360.0 - 180.0)
                    assert d <= 1e-6, f"TEXT rotation {a.rotation} != {e.rotation} for {e.text!r}"
        assert not unmatched, f"{kind}: {len(unmatched)} unmatched actual primitives"


def grid_intersections_from_lines(doc, layer_pred: Callable[[str], bool],
                                  *, ang_tol_deg: float = 1.0,
                                  min_length: float = 0.0) -> list[tuple[float, float]]:
    """Intersections of world-space LINE grid lines on matching layers.

    Collinear segments are merged, segments are split into two direction
    families (>= 20 degrees apart), and every cross-family pair of infinite
    lines is intersected.  Extra long lines on the layer (e.g. the synthetic
    decoy) produce extra intersections; callers compare as a subset.
    """
    lines = []  # (point, unit_dir, length)
    for e in disassemble.recursive_decompose(doc.modelspace()):
        if e.dxftype() != "LINE" or not layer_pred(e.dxf.layer):
            continue
        s = np.array([e.dxf.start.x, e.dxf.start.y], float)
        t = np.array([e.dxf.end.x, e.dxf.end.y], float)
        v = t - s
        n = float(np.hypot(*v))
        if n <= max(min_length, 1e-12):
            continue
        lines.append((s, v / n, n))
    if not lines:
        return []
    # direction families by angle mod 180
    ang = lambda d: math.degrees(math.atan2(d[1], d[0])) % 180.0
    ref = ang(lines[0][1])
    fam_a, fam_b = [], []
    for ln in lines:
        diff = abs((ang(ln[1]) - ref + 90.0) % 180.0 - 90.0)
        (fam_a if diff <= 20.0 else fam_b).append(ln)

    def merge(fam):
        out = []  # (point, dir)
        for s, d, _ in fam:
            nrm = np.array([-d[1], d[0]])
            off = float(nrm @ s)
            for (ps, pd) in out:
                pn = np.array([-pd[1], pd[0]])
                if abs(abs(float(pn @ nrm)) - 1.0) <= math.radians(ang_tol_deg) ** 2 and \
                        abs(float(pn @ s) - float(pn @ ps)) <= 1e-6 * max(1.0, abs(off)) + 1e-6:
                    break
            else:
                out.append((s, d))
        return out

    A, B = merge(fam_a), merge(fam_b)
    pts = []
    for (p1, d1) in A:
        for (p2, d2) in B:
            M = np.array([d1, -d2]).T
            if abs(np.linalg.det(M)) < 1e-9:
                continue
            t = np.linalg.solve(M, p2 - p1)
            q = p1 + t[0] * d1
            pts.append((float(q[0]), float(q[1])))
    return pts
