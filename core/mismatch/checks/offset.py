#!/usr/bin/env python3
"""core/mismatch/checks/offset.py — the cross-drawing **offset** check.

The other four checks ask "is this bolt in this hole?".  This one asks a
different question: **"these two things are supposed to line up — how far
apart are they?"**  A MECH duct edge against the STR member it should sit
on; a MECH handrail against the STR handrail; a plate against a plate.

What may be compared is decided by the **rules** (``offset_rules`` in the
policy, and in the profile that describes the drawing).  A rule says which
layers or families each side contributes, how far apart two items may be
and still count, and what an offset means.  **Nothing is compared that no
rule pairs** — that is the whole safety property of this check: it never
invents a comparison between two unrelated things.

The rules and their numbers
---------------------------

``search_mm``       the largest perpendicular offset two edges may have and
                    still be paired at all.
``tolerance_mm``    an offset at or below this is **within tolerance**:
                    the pair is counted as matched and *not* reported.  An
                    exact match (well under a millimetre) is the easy case
                    of this.
``warn_mm``         above this the finding is a WARNING; above
``critical_mm``     it is a CRITICAL.
``min_overlap_mm``  the two edges must run alongside each other for at
                    least this long — a 20 mm overlap of two 8 m edges is
                    not a match, it is a crossing.
``needs_review``    the rule is provisional; everything it finds is marked.

Two things this check refuses to do
-----------------------------------

**It never uses the drawing's own control points.**  A drawing marks its
grid intersections with small circles, and the fit is solved *from* those
intersections.  A circle that sits on a grid intersection is therefore not
an object to be compared with anything — it is the alignment itself.  Such
circles are excluded from every rule (and so are circles on a grid layer).

**It does not guess.**  When two STR candidates are equally good for one
MECH edge, the match is reported as *ambiguous* and the MECH item is left
unpaired, rather than one of them being picked.

Reporting
---------

Each match reports the rule, the offset vector, the perpendicular offset,
the overlap, both sources (handle, block, layer), the classification and
the confidence:

``position_shift``       the MECH item sits beside the STR one
``dimension_difference`` the MECH item is the right size but the wrong
                         place — two opposite edges offset by equal and
                         opposite amounts, so the *width* differs
"""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np

from core.spatial import GridIndex

from ..model import EntityRef, Finding

__all__ = ["run", "OFFSET_KIND", "WIDTH_KIND"]

#: The kinds this check reports.
OFFSET_KIND = "OFFSET"
WIDTH_KIND = "WIDTH_DIFFERENCE"
GAP_KIND = "GAP_DIFFERENCE"

#: An ambiguous match is not a defect — it is a question for a human.
REVIEW_SEVERITY = "REVIEW"

#: Two offsets this close are the same offset.
CLUSTER_TOL_MM = 0.5

#: Two segments count as parallel when their directions differ by less than
#: this (degrees, modulo 180°).
PARALLEL_TOL_DEG = 0.5

#: A circle this close to a grid intersection is the drawing's own control
#: point, not an object.
GRID_MARKER_MM = 1.0

#: Two candidates whose offsets differ by less than this are equally good.
AMBIGUITY_MM = 1.0

#: The overlap two edges must share, as a fraction of the shorter one.
MIN_OVERLAP_FRAC = 0.3

#: The layers a drawing marks its grid on (core.grid knows them).
GRID_LAYERS = {
    "CEN", "CENETR LINE", "CENTER", "PHANTOM", "PANTHOM",
    "HIDDEN", "HIDDDEN", "CENTERLINE", "C-ROAD-STAN",
}


# ─────────────────────────────────────────────────────────────────────────────
# candidates
# ─────────────────────────────────────────────────────────────────────────────

def _selector_matches(sel: dict, feature) -> bool:
    """Does a rule's selector claim this feature?"""
    layers = [str(s).upper() for s in (sel.get("layers") or ())]
    if "*" in layers:
        return True
    fams = set(feature.families or ())
    if fams & set(sel.get("families") or ()):
        return True
    return bool(layers) and str(feature.layer or "").upper() in layers


def _is_grid_marker(env, feature) -> bool:
    """Is this circle one of the drawing's own grid control points?

    Such a circle sits on a grid intersection — the fit was solved from
    those points — so comparing it with anything would be comparing the
    drawing with itself.  Circles on a grid layer are excluded for the
    same reason.
    """
    if feature.kind != "circle":
        return False
    if str(feature.layer or "").upper() in GRID_LAYERS:
        return True
    fit = getattr(env, "fit", None)
    points = getattr(fit, "points", None) if fit is not None else None
    if points is None:
        return False
    mm = float(env.mm or 1.0)
    limit = GRID_MARKER_MM / mm if mm else GRID_MARKER_MM
    for p in points:
        dx = float(p[0]) - float(feature.cx)
        dy = float(p[1]) - float(feature.cy)
        if math.hypot(dx, dy) <= limit:
            return True
    return False


#: The kinds a layer-selected candidate may have.
_LAYER_KINDS = ("rect", "polygon", "circle", "band", "point", "insert")


def _layer_features(env, source: str, layers) -> list:
    """The features of a drawing's **layers**, extracted on their own.

    The main extraction only keeps geometry a family claims, so a
    rectangle on a layer no family claims is invisible to it.  A rule that
    selects by layer needs that geometry, so it is extracted here, with a
    family of its own that claims exactly those layers.
    """
    import re as _re

    from ..classify import Classifier
    from ..features import extract
    from ..policy import Family

    doc = env.mech_doc if source == "MECH" else env.str_doc
    if doc is None:
        return []
    names = [str(l) for l in layers if str(l) != "*"]
    if "*" in [str(l) for l in layers]:
        pattern = ".*"
    elif names:
        pattern = "|".join(_re.escape(n) for n in names)
    else:
        return []
    fam = Family(name="__offset_layers__", src=source,
                 features=_LAYER_KINDS, layer_regex=pattern, block_regex=None,
                 size_min_mm=None, size_max_mm=None, size_key="w")
    pol = env.policy.replace(families=tuple(env.policy.families) + (fam,))
    clf = Classifier(pol)
    root = env.mech_root if source == "MECH" else env.str_root
    fs = extract(doc, source=source, policy=pol, classifier=clf,
                 mm_per_unit=float(env.mm or 1.0), root=root, bands=False)
    return list(fs.features)


def _grid_bay(env, x: float, y: float) -> str | None:
    """The grid bay a point is in — ``"A-1"`` means between A and 1.

    The bays come from the fit's own correspondences: the grid
    intersections the transform was solved from.
    """
    fit = getattr(env, "fit", None)
    points = getattr(fit, "points", None) if fit is not None else None
    labels = tuple(getattr(fit, "labels", ()) or ())
    if points is None or len(points) == 0:
        return None
    mm = float(env.mm or 1.0)
    xs = sorted((float(p[0]), i) for i, p in enumerate(points))
    ys = sorted((float(p[1]), i) for i, p in enumerate(points))
    below_x = [v for v in xs if v[0] <= x]
    above_x = [v for v in xs if v[0] > x]
    below_y = [v for v in ys if v[0] <= y]
    above_y = [v for v in ys if v[0] > y]
    if not (below_x and above_x and below_y and above_y):
        return None

    def _parts(entry):
        """The two halves of a correspondence's label ("1-A" → ("1", "A"))."""
        try:
            return str(labels[entry[1]]).split("-")
        except (IndexError, TypeError):
            return []

    def _letter(entry):
        """The axis letter of a correspondence.

        The halves of the label are a letter and a number, but which comes
        first is the drawing's business — so the letter is the one that is
        not a number.
        """
        for part in _parts(entry):
            if part and not part.isdigit():
                return part
        return None

    def _digit(entry):
        """The axis number of a correspondence."""
        for part in _parts(entry):
            if part and part.isdigit():
                return part
        return None

    lx, ux = _letter(below_x[-1]), _letter(above_x[0])
    ly, uy = _digit(below_y[-1]), _digit(above_y[0])
    if not all((lx, ux, ly, uy)):
        return None
    # the grid's own order: the letters A, B, C … and the numbers 1, 2, 3 …
    letters = sorted({lx, ux})
    numbers = sorted({ly, uy}, key=lambda v: int(v) if v.isdigit() else 0)
    return f"{'-'.join(letters)} / {'-'.join(numbers)}"


def _candidates(env, rule, source: str) -> list:
    """The features of one side that a rule may compare."""
    fs = env.mech if source == "MECH" else env.str
    sel = rule.mech_selector if source == "MECH" else rule.str_selector
    layers = [str(s) for s in (sel.get("layers") or ())]
    fams = [str(s) for s in (sel.get("families") or ())]
    out = []
    if fams:
        for f in fs.features:
            if set(f.families or ()) & set(fams):
                out.append(f)
    if layers:
        for f in _layer_features(env, source, layers):
            out.append(f)
    elif not fams:
        return []
    return [f for f in out if not _is_grid_marker(env, f)]


# ─────────────────────────────────────────────────────────────────────────────
# edges
# ─────────────────────────────────────────────────────────────────────────────

def _edges(feature) -> list:
    """The edges of a feature, as ``(ax, ay, bx, by)`` in world units."""
    k = feature.kind
    if k == "rect":
        cx, cy = float(feature.cx), float(feature.cy)
        ux, uy = float(feature.ux), float(feature.uy)
        hw = 0.5 * float(feature.w)
        hh = 0.5 * float(feature.h)
        nx, ny = -uy, ux
        c = [(cx - hw * ux - hh * nx, cy - hw * uy - hh * ny),
             (cx + hw * ux - hh * nx, cy + hw * uy - hh * ny),
             (cx + hw * ux + hh * nx, cy + hw * uy + hh * ny),
             (cx - hw * ux + hh * nx, cy - hw * uy + hh * ny)]
    elif k == "polygon":
        pts = [(float(p[0]), float(p[1])) for p in (feature.points or ())]
        if len(pts) < 3:
            return []
        c = pts
    elif k == "band":
        ax, ay = float(feature.a[0]), float(feature.a[1])
        bx, by = float(feature.b[0]), float(feature.b[1])
        ux, uy = float(feature.ux), float(feature.uy)
        hw = float(feature.hw)
        nx, ny = -uy, ux
        c = [(ax - hw * nx, ay - hw * ny), (bx - hw * nx, by - hw * ny),
             (bx + hw * nx, by + hw * ny), (ax + hw * nx, ay + hw * ny)]
    else:
        return []
    return [(c[i][0], c[i][1], c[(i + 1) % len(c)][0], c[(i + 1) % len(c)][1])
            for i in range(len(c))]


def _edge_box(e) -> tuple:
    ax, ay, bx, by = e
    return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))


def _edge_len(e) -> float:
    ax, ay, bx, by = e
    return math.hypot(bx - ax, by - ay)


def _edge_dir(e) -> tuple:
    ax, ay, bx, by = e
    n = math.hypot(bx - ax, by - ay)
    if n <= 1e-12:
        return (1.0, 0.0)
    return ((bx - ax) / n, (by - ay) / n)


def _parallel(e1, e2) -> bool:
    """Are two edges parallel (their directions within 0.5°)?"""
    u1 = _edge_dir(e1)
    u2 = _edge_dir(e2)
    dot = abs(u1[0] * u2[0] + u1[1] * u2[1])
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(math.acos(dot)) <= PARALLEL_TOL_DEG


def _overlap(e1, e2) -> float:
    """How long the two edges run alongside each other."""
    u = _edge_dir(e1)
    a1 = (e1[0] * u[0] + e1[1] * u[1], e1[2] * u[0] + e1[3] * u[1])
    a2 = (e2[0] * u[0] + e2[1] * u[1], e2[2] * u[0] + e2[3] * u[1])
    lo = max(min(a1[0], a1[1]), min(a2[0], a2[1]))
    hi = min(max(a1[0], a1[1]), max(a2[0], a2[1]))
    return max(0.0, hi - lo)


def _offset(e1, e2) -> tuple:
    """The signed perpendicular offset from ``e2`` to ``e1``, and the vector.

    Returns ``(signed, (dx, dy))`` where ``signed`` is positive when the
    first edge lies on the left of the second one's direction, and the
    vector is the **perpendicular displacement** — how far the first edge
    is from the second one, across the gap, not from corner to corner.
    """
    u2 = _edge_dir(e2)
    n2 = (-u2[1], u2[0])
    dx = e1[0] - e2[0]
    dy = e1[1] - e2[1]
    signed = dx * n2[0] + dy * n2[1]
    return signed, (signed * n2[0], signed * n2[1])


# ─────────────────────────────────────────────────────────────────────────────
# the matching
# ─────────────────────────────────────────────────────────────────────────────

def _match_edges(rule, mech_edges, str_edges, mm: float):
    """Pair the edges, nearest first, one to one.

    Returns ``(pairs, ambiguous, unmatched_mech, unmatched_str)`` where a
    pair is ``(mech_index, str_index, signed_mm, (dx_mm, dy_mm), overlap_mm)``.
    """
    search = float(rule.search_mm)
    min_overlap = max(float(rule.min_overlap_mm),
                      MIN_OVERLAP_FRAC * min((_edge_len(e) for e in mech_edges),
                                              default=0.0))
    if not mech_edges or not str_edges:
        return [], [], list(range(len(mech_edges))), list(range(len(str_edges)))

    boxes = np.asarray([_edge_box(e) for e in str_edges], dtype=float)
    index = GridIndex(boxes, cell=max(search, 1.0))

    # every (mech, str) pair that is close enough and parallel enough
    proposals = []
    for i, me in enumerate(mech_edges):
        bx = _edge_box(me)
        grown = (bx[0] - search, bx[1] - search, bx[2] + search, bx[3] + search)
        for j in index.query_bbox(grown):
            se = str_edges[int(j)]
            if not _parallel(me, se):
                continue
            signed, vec = _offset(me, se)
            dist = abs(signed) * mm
            if dist > search:
                continue
            overlap = _overlap(me, se) * mm
            if overlap < min_overlap:
                continue
            proposals.append((dist, i, int(j), signed * mm, vec, overlap))

    proposals.sort(key=lambda p: (p[0], p[1], p[2]))

    # the candidates of each MECH edge, best first — so "is there another
    # candidate just as good?" is a look at the next one, not a scan of
    # every proposal
    by_mech: dict[int, list] = {}
    for prop in proposals:
        by_mech.setdefault(prop[1], []).append(prop)

    used_mech: set[int] = set()
    used_str: set[int] = set()
    pairs = []
    ambiguous = []
    for i, cands in by_mech.items():
        if not cands or i in used_mech:
            continue
        # the candidates of this MECH edge, best first
        best = cands[0]
        dist, _i, j, signed, vec, overlap = best
        # is another candidate just as good?  That is decided on the
        # candidates themselves, before any of them is consumed — otherwise
        # the one-to-one pass could quietly resolve an ambiguity.
        rivals = [c[2] for c in cands[1:]
                  if abs(c[0] - dist) <= AMBIGUITY_MM]
        if rivals:
            # not paired — and so it is *unpaired*: the MECH edge stays in
            # the unmatched list, because nothing was resolved for it
            ambiguous.append((i, j, signed, vec, overlap, rivals))
            continue
        # the nearest candidate that is still free
        partner = None
        for c in cands:
            if c[2] not in used_str:
                partner = c
                break
        if partner is None:
            continue
        used_mech.add(i)
        used_str.add(partner[2])
        pairs.append((i, partner[2], partner[3], partner[4], partner[5]))

    unmatched_mech = [i for i in range(len(mech_edges)) if i not in used_mech]
    unmatched_str = [j for j in range(len(str_edges)) if j not in used_str]
    return pairs, ambiguous, unmatched_mech, unmatched_str


# ─────────────────────────────────────────────────────────────────────────────
# the finding
# ─────────────────────────────────────────────────────────────────────────────

def _ref(feature, family: str) -> EntityRef:
    return feature.entity_ref(family)


def _severity(rule, dist: float) -> str:
    if dist > float(rule.critical_mm):
        return "CRITICAL"
    if dist > float(rule.warn_mm):
        return "WARNING"
    return "INFO"


def _source(feature, family: str) -> dict:
    return {"handle": feature.handle, "block": feature.block,
            "layer": feature.layer, "family": family}


# ─────────────────────────────────────────────────────────────────────────────
# the check
# ─────────────────────────────────────────────────────────────────────────────

def run(env) -> list[Finding]:
    """Every offset finding of one run."""
    out: list[Finding] = []
    notes = getattr(env, "rule_notes", None)
    if notes is None:
        notes = {}
        try:
            env.rule_notes = notes
        except Exception:
            pass

    for rule in getattr(env.policy, "offset_rules", ()) or ():
        if not getattr(rule, "enabled", True):
            continue
        if rule.method != "edge_to_edge":
            # The other methods are part of the schema; only edge_to_edge
            # is implemented (and only edge_to_edge is enabled anywhere).
            continue
        mech_feats = _candidates(env, rule, "MECH")
        str_feats = _candidates(env, rule, "STR")
        mech_edges, str_edges = [], []
        mech_owner, str_owner = [], []
        mech_slot, str_slot = [], []
        for f in mech_feats:
            for n, e in enumerate(_edges(f)):
                mech_edges.append(e)
                mech_owner.append(f)
                mech_slot.append(n)
        for f in str_feats:
            for n, e in enumerate(_edges(f)):
                str_edges.append(e)
                str_owner.append(f)
                str_slot.append(n)
        if not mech_edges or not str_edges:
            notes[rule.name] = {"pairs": 0, "ambiguous": 0,
                                "unmatched_mech": len(mech_edges),
                                "unmatched_str": len(str_edges)}
            continue

        mm = float(env.mm or 1.0)
        pairs, ambiguous, unmatched_mech, unmatched_str = _match_edges(
            rule, mech_edges, str_edges, mm)

        findings = _report(env, rule, pairs, ambiguous, mech_edges, str_edges,
                          mech_owner, str_owner, mech_slot, str_slot, mm)
        out += findings

        # the unmatched are counted in **objects**, not edges: an item
        # whose four edges all failed to pair is one unmatched item.
        unmatched_mech_objs = {id(mech_owner[i]) for i in unmatched_mech}
        unmatched_str_objs = {id(str_owner[j]) for j in unmatched_str}
        notes[rule.name] = {
            "pairs": len(pairs),
            # the counts are of *items*, like the findings: one object with
            # four ambiguous edges is one unresolved thing
            "ambiguous": sum(1 for f in findings
                             if f.details.get("ambiguous")),
            "ambiguous_edges": len(ambiguous),
            "unmatched_mech": len(unmatched_mech_objs),
            "unmatched_str": len(unmatched_str_objs),
            "unmatched_mech_edges": len(unmatched_mech),
            "unmatched_str_edges": len(unmatched_str),
            "reported": len(findings),
            "note": ("items offset by more than search_mm "
                     f"({float(rule.search_mm):g} mm) are not compared"),
        }
    return out


def _side(mech_edge, str_edge, str_centre) -> float:
    """Which side of the STR edge the MECH edge lies on.

    Positive means *inside* the STR outline.  Two MECH edges on the same
    side of their STR partners mean the MECH item is the wrong **width**;
    two on opposite sides mean it is in the wrong **place**.
    """
    nx, ny = _inward_normal(str_edge, str_centre)
    mx = 0.5 * (mech_edge[0] + mech_edge[2])
    my = 0.5 * (mech_edge[1] + mech_edge[3])
    sx = 0.5 * (str_edge[0] + str_edge[2])
    sy = 0.5 * (str_edge[1] + str_edge[3])
    return (mx - sx) * nx + (my - sy) * ny


def _inward_normal(edge, centre) -> tuple:
    """The unit normal of an edge, pointing towards the outline's centre."""
    ax, ay, bx, by = edge
    ux, uy = _edge_dir(edge)
    nx, ny = -uy, ux
    mx = 0.5 * (ax + bx)
    my = 0.5 * (ay + by)
    if (centre[0] - mx) * nx + (centre[1] - my) * ny < 0.0:
        nx, ny = -nx, -ny
    return nx, ny


def _report(env, rule, pairs, ambiguous, mech_edges, str_edges, mech_owner,
            str_owner, mech_slot, str_slot, mm):
    """One finding per object pair, with the classification.

    A rectangle offset by 7.5 mm has four edge pairs; that is **one**
    defect, not four.  So the edge pairs are grouped by the objects they
    belong to, and each group becomes one finding.
    """
    # ── one candidate per (MECH object, STR object) pair ───────────────
    groups: dict[tuple[int, int], list] = {}
    for idx, (i, j, signed, vec, overlap) in enumerate(pairs):
        groups.setdefault((id(mech_owner[i]), id(str_owner[j])), []).append(
            {"i": i, "j": j, "signed": signed, "vec": vec, "overlap": overlap})

    candidates = []
    for (_mk, _sk), edges in groups.items():
        best = max(edges, key=lambda e: abs(e["signed"]))
        candidates.append({
            "mech": mech_owner[best["i"]],
            "str": str_owner[best["j"]],
            "edges": edges,
            "best": best,
            "offset": abs(best["signed"]),
            "signed": best["signed"],
            "classification": "position_shift",
            "total": None,
            "ambiguous": False,
        })

    # An unresolved match is a candidate too: it may be one half of a gap
    # difference, and it must never be reported as a defect.
    amb_groups: dict[tuple[int, int], list] = {}
    for entry in ambiguous:
        i, j, signed, vec, overlap, rivals = entry
        amb_groups.setdefault((id(mech_owner[i]), id(str_owner[j])), []).append({
            "i": i, "j": j, "signed": signed, "vec": vec, "overlap": overlap,
            "rivals": rivals,
        })
    for (_mk, _sk), edges in amb_groups.items():
        best = max(edges, key=lambda e: abs(e["signed"]))
        candidates.append({
            "mech": mech_owner[best["i"]],
            "str": str_owner[best["j"]],
            "edges": edges,
            "best": best,
            "offset": abs(best["signed"]),
            "signed": best["signed"],
            "classification": "ambiguous",
            "total": None,
            "ambiguous": True,
            "rivals": sorted({r for e in edges for r in e.get("rivals", ())}),
        })

    # ── two opposite MECH edges inside the same STR object: a width ──
    candidates = _merge_width_differences(candidates, mech_owner, str_owner,
                                         mech_slot, str_slot, mech_edges,
                                         str_edges)

    # ── a gap difference beats two position shifts ────────────────────
    candidates = _merge_gap_differences(candidates, mech_owner, str_owner,
                                        mech_slot, str_slot, mech_edges,
                                        str_edges, mm, rule)

    # ── the same STR part twice with the same offset is one thing ─────
    candidates = _merge_same_str(candidates)

    findings: list[Finding] = []
    for cand in candidates:
        mf, sf = cand["mech"], cand["str"]
        fam_m = (mf.families or ("",))[0]
        fam_s = (sf.families or ("",))[0]
        dist = cand["total"] if cand["total"] is not None else cand["offset"]
        best = cand["best"]
        if cand["total"] is None and dist <= float(rule.tolerance_mm):
            # within tolerance — matched, and not reported
            continue
        if cand["classification"] == "dimension_difference":
            # a width: the MECH outline is not as wide as the STR one
            findings.append(Finding(
                kind=WIDTH_KIND,
                severity=_severity(rule, dist),
                origin="mismatch",
                mech_ref=_ref(mf, fam_m),
                str_ref=_ref(sf, fam_s),
                location=mf.centre(),
                str_location=sf.centre(),
                offset_mm=(0.0, 0.0),
                magnitude_mm=dist,
                tolerance_mm=float(rule.tolerance_mm),
                details={
                    "rule": rule.name,
                    "classification": "dimension_difference",
                    "width_difference_mm": dist,
                    "edge_offsets_mm": [e["signed"] for e in cand["edges"]],
                    "overlap_mm": min(e["overlap"] for e in cand["edges"]),
                    "confidence": "normal",
                    "ambiguous": False,
                    "needs_review": bool(rule.needs_review),
                    "grid_bay": _grid_bay(env, mf.cx, mf.cy),
                    "mech": _source(mf, fam_m),
                    "str": _source(sf, fam_s),
                },
                message=(f"{rule.name}: the MECH {fam_m or 'item'} is "
                         f"{dist:.1f} mm narrower than the STR "
                         f"{fam_s or 'item'}"),
            ))
            continue
        if cand["classification"] == "gap_difference":
            # a gap: the MECH outline is wider (or narrower) than the STR
            # gap between two parts, by the *total* of the two offsets
            findings.append(Finding(
                kind=GAP_KIND,
                severity=(REVIEW_SEVERITY if cand.get("ambiguous")
                          else _severity(rule, dist)),
                origin="mismatch",
                mech_ref=_ref(mf, fam_m),
                str_ref=_ref(sf, fam_s),
                location=mf.centre(),
                str_location=sf.centre(),
                offset_mm=(0.0, 0.0),
                magnitude_mm=dist,
                tolerance_mm=float(rule.tolerance_mm),
                details={
                    "rule": rule.name,
                    "classification": "gap_difference",
                    "confidence": ("ambiguous" if cand.get("ambiguous")
                                   else "normal"),
                    "ambiguous": bool(cand.get("ambiguous")),
                    "gap_mm": dist,
                    "edge_offsets_mm": [e["signed"] for e in cand["edges"]],
                    "str_parts": _sources(cand["edges"], str_owner),
                    "overlap_mm": min(e["overlap"] for e in cand["edges"]),
                    "confidence": "normal",
                    "ambiguous": False,
                    "needs_review": bool(rule.needs_review),
                    "grid_bay": _grid_bay(env, mf.cx, mf.cy),
                    "mech": _source(mf, fam_m),
                    "str": _source(sf, fam_s),
                },
                message=(f"{rule.name}: the MECH {fam_m or 'item'} is "
                         f"{dist:.1f} mm wider than the gap between the two "
                         f"STR parts"),
            ))
            continue
        ambiguous_cand = bool(cand.get("ambiguous"))
        findings.append(Finding(
            kind=OFFSET_KIND,
            severity=REVIEW_SEVERITY if ambiguous_cand else _severity(rule, dist),
            origin="mismatch",
            mech_ref=_ref(mf, fam_m),
            str_ref=_ref(sf, fam_s),
            location=mf.centre(),
            str_location=sf.centre(),
            offset_mm=(best["vec"][0] * mm, best["vec"][1] * mm),
            magnitude_mm=dist,
            tolerance_mm=float(rule.tolerance_mm),
            details={
                "rule": rule.name,
                "classification": ("ambiguous" if ambiguous_cand
                                   else cand["classification"]),
                "perpendicular_mm": best["signed"],
                "overlap_mm": max(e["overlap"] for e in cand["edges"]),
                "edge_count": len(cand["edges"]),
                "evidence": _sources(cand["edges"], str_owner),
                "confidence": "ambiguous" if ambiguous_cand else "normal",
                "ambiguous": ambiguous_cand,
                "candidates": cand.get("rivals") or [],
                "needs_review": bool(rule.needs_review),
                "grid_bay": _grid_bay(env, mf.cx, mf.cy),
                "mech": _source(mf, fam_m),
                "str": _source(sf, fam_s),
            },
            message=(f"{rule.name}: the MECH {fam_m or 'item'} is "
                     f"{dist:.1f} mm from the STR {fam_s or 'item'}"
                     + (" — not resolved" if ambiguous_cand else "")),
        ))

    return findings


def cluster(findings, tol: float = CLUSTER_TOL_MM) -> list:
    """The findings whose offsets agree, as one cluster each.

    A systematic error — the whole drawing is 23.7 mm out — shows up as
    one number, not as forty.  The cluster says how many findings share
    the offset and which grid bays they are in.
    """
    out: list = []
    # every *observation*: a finding's own offset, and — for a finding that
    # merged several edges — each of the edge offsets it stands for.  The
    # cluster table is about what was measured, not about how it was
    # grouped into findings.
    bay_of = {f.id: (f.details or {}).get("grid_bay") for f in findings}
    rule_of = {f.id: (f.details or {}).get("rule") for f in findings}
    observations: list[tuple[float, str]] = []
    for f in findings:
        observations.append((float(f.magnitude_mm or 0.0), f.id))
        for part in ((f.details or {}).get("edge_offsets_mm") or ()):
            observations.append((abs(float(part)), f.id))
    for value, fid in sorted(observations, key=lambda o: -o[0]):
        for c in out:
            if abs(c["offset_mm"] - value) <= tol:
                c["count"] += 1
                c["finding_ids"].append(fid)
                bay = bay_of.get(fid)
                if bay and bay not in c["bays"]:
                    c["bays"].append(bay)
                rule = rule_of.get(fid)
                if rule and rule not in c["rules"]:
                    c["rules"].append(rule)
                break
        else:
            out.append({
                "offset_mm": round(value, 3),
                "count": 1,
                "finding_ids": [fid],
                "bays": [b for b in [bay_of.get(fid)] if b],
                "rules": [r for r in [rule_of.get(fid)] if r],
            })
    for c in out:
        c["bays"].sort()
    return out


def _sources(edges, str_owner) -> list:
    """The STR parts that took part, as evidence."""
    out = []
    seen = set()
    for e in edges:
        sf = str_owner[e["j"]]
        key = (sf.handle, sf.block, sf.layer,
               round(float(sf.cx), 3), round(float(sf.cy), 3))
        if key in seen:
            continue
        seen.add(key)
        out.append({"handle": sf.handle, "block": sf.block,
                    "layer": sf.layer, "signed_mm": e["signed"],
                    "overlap_mm": e["overlap"]})
    return out


def _merge_width_differences(candidates, mech_owner, str_owner, mech_slot,
                              str_slot, mech_edges, str_edges):
    """Two opposite MECH edges inside one STR object: the **width** differs.

    Both MECH edges sit on the same side of their STR partner, so the
    MECH item is the right shape in the wrong place — its width is not the
    STR's.  That is one finding with the width difference, not two
    position shifts.
    """
    out: list = []
    for cand in candidates:
        edges = cand["edges"]
        found = None
        for a in range(len(edges)):
            for b in range(a + 1, len(edges)):
                ea, eb = edges[a], edges[b]
                i1, i2 = ea["i"], eb["i"]
                j1, j2 = ea["j"], eb["j"]
                if not _opposite(mech_slot[i1], mech_slot[i2]):
                    continue
                if not _opposite(str_slot[j1], str_slot[j2]):
                    continue
                d1 = _side(mech_edges[i1], str_edges[j1],
                           str_owner[j1].centre())
                d2 = _side(mech_edges[i2], str_edges[j2],
                           str_owner[j2].centre())
                if d1 * d2 <= 0.0:
                    continue          # opposite sides → a position shift
                found = abs(d1) + abs(d2)
                break
            if found is not None:
                break
        if found is not None:
            cand = dict(cand)
            cand["classification"] = "dimension_difference"
            cand["total"] = found
        out.append(cand)
    return out


def _merge_gap_differences(candidates, mech_owner, str_owner, mech_slot,
                            str_slot, mech_edges, str_edges, mm, rule=None):
    """Two opposite MECH edges against two different STR parts.

    When the two opposite edges of one MECH outline are each offset by the
    same amount — and in opposite directions — from two *different* STR
    parts, the two parts do not meet the MECH: the gap between them is
    wrong.  That is **one** finding with the total gap, not two position
    shifts.
    """
    # The MECH side is grouped by the **kind of thing**: a pair of the
    # same object (the same block) is what a gap difference is about —
    # two MECH items against two adjacent STR parts.
    by_mech: dict[str, list] = {}
    for cand in candidates:
        by_mech.setdefault(cand["mech"].block or "", []).append(cand)

    # every valid gap pair, the biggest first — so the one that matters
    # is not lost to a trivial one found earlier
    found: list = []
    for group in by_mech.values():
        for a in range(len(group)):
            for b in range(a + 1, len(group)):
                c1, c2 = group[a], group[b]
                if c1["str"] is c2["str"]:
                    continue                      # the same STR part
                # the two MECH items must be the same kind of thing — a
                # pair of the same object
                if not (c1["mech"].block or ""):
                    continue                      # not an object, just a shape
                if (c1["mech"].block or "") != (c2["mech"].block or ""):
                    continue
                i1 = c1["best"]["i"]
                i2 = c2["best"]["i"]
                j1 = c1["best"]["j"]
                j2 = c2["best"]["j"]
                # the two offsets must be the same amount: the pair is
                # squeezed (or stretched) against the gap between the two
                # STR parts, and the gap is off by the *total* of the two
                s1, s2 = c1["signed"], c2["signed"]
                if abs(abs(s1) - abs(s2)) > 0.5:
                    continue                      # not the same amount
                total = abs(s1) + abs(s2)
                floor = float(rule.warn_mm) if rule is not None else 0.5
                if total <= max(0.5, floor * 0.5):
                    continue
                found.append((total, c1, c2))
    found.sort(key=lambda f: -f[0])

    consumed: set[int] = set()
    merged: list = []
    for total, c1, c2 in found:
        if id(c1) in consumed or id(c2) in consumed:
            continue
        merged.append({
            "mech": c1["mech"],
            "str": c1["str"],
            "edges": list(c1["edges"]) + list(c2["edges"]),
            "best": c1["best"],
            "offset": total,
            "signed": c1["signed"],
            "classification": "gap_difference",
            "total": total,
            "ambiguous": bool(c1.get("ambiguous") or c2.get("ambiguous")),
        })
        consumed.add(id(c1))
        consumed.add(id(c2))
    kept = [c for c in candidates if id(c) not in consumed]
    return kept + merged


def _merge_same_str(candidates):
    """The same STR part, the same offset, twice → one finding.

    A part that appears twice in the drawing (or is matched by two MECH
    edges) is **one** thing; the two are merged and both edges are kept as
    the evidence.
    """
    out: list = []
    by_str: dict[tuple, list] = {}
    for cand in candidates:
        sf = cand["str"]
        # the same *part* — the same block, drawn twice — is one thing
        if sf.block:
            key = (sf.block,)
        else:
            key = (sf.layer, round(float(sf.cx), 3), round(float(sf.cy), 3))
        by_str.setdefault(key, []).append(cand)
    for group in by_str.values():
        group.sort(key=lambda c: -c["offset"])
        keep = group[0]
        merged = dict(keep)
        merged["edges"] = list(keep["edges"])
        for other in group[1:]:
            if abs(other["offset"] - keep["offset"]) <= 0.5:
                merged["edges"] = merged["edges"] + list(other["edges"])
            else:
                out.append(other)
        out.append(merged)
    return out


def _width_difference(group, mech_edges, str_edges, mech_owner, str_owner,
                      mech_slot, str_slot, mm):
    """``(delta, d1, d2)`` when the group is a width difference, else None.

    Two **opposite** edges of one MECH outline, each sitting the same side
    of its STR partner, mean the MECH item is the right shape in the wrong
    place — the *width* differs.  Two on opposite sides mean it is simply
    displaced.
    """
    if len(group) < 2:
        return None
    best = None
    for a in range(len(group)):
        for b in range(a + 1, len(group)):
            ia, i1, j1, sa, va, oa = group[a]
            ib, i2, j2, sb, vb, ob = group[b]
            if mech_owner[i1] is not mech_owner[i2]:
                continue
            if str_owner[j1] is not str_owner[j2]:
                continue
            if not _opposite(mech_slot[i1], mech_slot[i2]):
                continue
            if not _opposite(str_slot[j1], str_slot[j2]):
                continue
            d1 = _side(mech_edges[i1], str_edges[j1], str_owner[j1].centre())
            d2 = _side(mech_edges[i2], str_edges[j2], str_owner[j2].centre())
            if d1 * d2 <= 0.0:
                continue                      # opposite sides → a position shift
            delta = abs(d1) + abs(d2)
            if best is None or delta > best[0]:
                best = (delta, d1, d2)
    return best


def _opposite(slot_a: int, slot_b: int) -> bool:
    """Are two edges of the same outline opposite each other?

    A four-edge outline (a rectangle, a band) has its opposite edges two
    apart: 0 and 2, 1 and 3.
    """
    return (slot_a - slot_b) % 2 == 0 and slot_a != slot_b
