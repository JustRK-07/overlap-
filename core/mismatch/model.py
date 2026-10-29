#!/usr/bin/env python3
"""core/mismatch/model.py — the mismatch data model and its serialisers.

Step 8b's public contract.  Everything downstream (the checks, the stage,
the CSV/docx writers, the API and the UI) speaks in terms of these three
things:

:class:`EntityRef`
    *where a piece of geometry is* — which drawing, which INSERT chain,
    which leaf handle, which layer, which family it was classified as.
    A ``Feature`` (not yet implemented, step 8b.3) carries one per
    extracted shape.
:class:`Finding`
    *what is wrong* — a kind, a severity, a MECH-world location, the
    measured offset (always ``STR − MECH``, i.e. the direction the MECH
    item would have to move), the tolerance it was judged against and a
    free-form ``details`` dict.
:func:`rank_findings` / :func:`truncate`
    the deterministic ordering.  Severity first, then worst ratio, then
    kind and position; ``id`` numbers are handed out *after* the sort so
    ``MM-0001`` is always the most severe finding of the run.

Conventions
-----------
* ``location`` is in **MECH world coordinates, drawing units** (not mm).
* Every ``*_mm`` field is in millimetres, computed with the MECH file's
  ``mm_per_unit``.  There are no unitless lengths anywhere.
* ``offset_mm`` is ``str_location − location`` in mm, i.e. *STR relative
  to MECH*.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

__all__ = [
    "EntityRef",
    "Finding",
    "KINDS",
    "SEVERITIES",
    "SEVERITY_RANK",
    "ORIGINS",
    "CSV_COLUMNS",
    "MESSAGE_TEMPLATES",
    "finding_uid",
    "format_message",
    "rank_key",
    "rank_findings",
    "truncate",
    "summarize",
    "finding_to_dict",
    "findings_to_dicts",
    "finding_row",
    "write_csv",
    "write_json",
]

# ── vocabularies ────────────────────────────────────────────────────────────

#: Every kind a check may emit.  Order is the tie-break order of
#: :func:`rank_key` only after severity and ratio, so it never reorders
#: findings of different severity.
KINDS = (
    "BOLT_HOLE_OFFSET",
    "MISSING_HOLE",
    "BOLT_OFF_PLATE",
    "PATTERN_OFFSET",
    "PATTERN_ROTATED",
    "PATTERN_MISFIT",
    "EXTRA_HOLE",
    "SUPPORT_OFF_MEMBER",
    "SUPPORT_POINT_OFF_MEMBER",
    "OUTSIDE_OUTLINE",
    "EDGE_DISTANCE",
    "CLASH",
    "CLEARANCE",
    "MISSING_COUNTERPART",
    "SYSTEMATIC_OFFSET",
)

#: Most severe first — this is also the numeric rank used for sorting.
#: ``REVIEW`` is not a defect: it is something a human has to look at —
#: an ambiguous match, or a finding from a provisional rule.  It is
#: deliberately **not** CRITICAL or WARNING, so it stays out of those
#: counts.
SEVERITIES = ("CRITICAL", "WARNING", "INFO", "REVIEW")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

#: ``mismatch`` = produced by this stage; ``spotcheck`` = promoted from a
#: ``core/spotcheck`` issue by ``core/findings`` (step 8b.5).
ORIGINS = ("mismatch", "spotcheck")

#: The exact header of ``out/findings.csv`` (PLAN_MISMATCH §4).
CSV_COLUMNS = (
    "id", "uid", "kind", "severity", "origin",
    "x", "y", "str_x", "str_y",
    "dx_mm", "dy_mm", "magnitude_mm",
    "local_dx_mm", "local_dy_mm", "local_magnitude_mm",
    "tolerance_mm", "tolerance_eff_mm", "ratio", "rotation_deg",
    "systematic", "within_fit_uncertainty", "confidence",
    "mech_handle", "mech_block", "mech_layer",
    "str_handle", "str_block", "str_layer",
    "message",
)

#: Human-readable one-liners.  ``format_message`` fills what it can and
#: leaves any placeholder the caller did not supply untouched, so a
#: partially-populated finding still renders something readable.
MESSAGE_TEMPLATES = {
    "BOLT_HOLE_OFFSET":
        "Bolt {mech_block} bolt #{mech_index} is {magnitude:.1f} mm off hole "
        "{str_block} (allowed {tolerance:.1f} mm)",
    "MISSING_HOLE":
        "Bolt {mech_block} bolt #{mech_index} has no matching hole in {str_refs} "
        "within {search:.0f} mm",
    "BOLT_OFF_PLATE":
        "Bolt {mech_block} bolt #{mech_index} is {magnitude:.1f} mm off plate "
        "{str_refs}",
    "PATTERN_OFFSET":
        "Bolt pattern {mech_block} ({n_bolts} bolts) is translated "
        "{magnitude:.1f} mm (allowed {tolerance:.1f} mm)",
    "PATTERN_ROTATED":
        "Bolt pattern {mech_block} ({n_bolts} bolts) is rotated "
        "{rotation:+.2f}° (allowed {tolerance:.1f} mm)",
    "PATTERN_MISFIT":
        "Bolt pattern {mech_block} ({n_bolts} bolts) does not fit "
        "{str_refs}: translated {magnitude:.1f} mm and rotated {rotation:+.2f}°",
    "EXTRA_HOLE":
        "Hole {str_block} in {str_refs} has no bolt within {search:.0f} mm",
    "SUPPORT_OFF_MEMBER":
        "Support {mech_block} is {magnitude:.1f} mm off member {str_refs} "
        "(allowed {allowed:.1f} mm, bearing {bearing:.0%})",
    "SUPPORT_POINT_OFF_MEMBER":
        "Support point {mech_block} is {magnitude:.1f} mm off member "
        "{str_refs} (allowed {allowed:.1f} mm)",
    "OUTSIDE_OUTLINE":
        "Footprint {mech_block} sticks {magnitude:.1f} mm outside {str_refs} "
        "(allowed {tolerance:.1f} mm)",
    "EDGE_DISTANCE":
        "Footprint {mech_block} is only {edge:.1f} mm from the edge of "
        "{str_refs} (minimum {tolerance:.1f} mm)",
    "CLASH":
        "{mech_block} clashes with {str_refs} by {magnitude:.1f} mm",
    "CLEARANCE":
        "{mech_block} clears {str_refs} by only {magnitude:.1f} mm "
        "(minimum {tolerance:.1f} mm)",
    "MISSING_COUNTERPART":
        "{mech_block} has no counterpart of family {counterpart} within "
        "{search:.0f} mm",
    "SYSTEMATIC_OFFSET":
        "{n_items} items are consistently shifted by {magnitude:.1f} mm — "
        "{reason}",
}


# ── the model ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EntityRef:
    """Where one classified piece of geometry lives, in one drawing."""

    source: str                                   # "MECH" | "STR"
    handle: str                                   # leaf entity handle (converted doc)
    insert_chain: tuple[str, ...] = ()            # block names, modelspace first
    block: str = ""                               # innermost block ("" = modelspace)
    root_block: str = ""                          # outermost block ("" = modelspace)
    layer: str = ""                               # effective layer, prefixes stripped
    family: str = ""
    feature: str = ""                             # circle|rect|polygon|band|point|pattern
    size_mm: dict = field(default_factory=dict)   # e.g. {"r": 10.0} / {"w": 300, "h": 300}
    in_overlay: bool | None = None                # survived the cleaned/ overlay?

    def key(self) -> str:
        """Stable identity string used to build a finding ``uid``."""
        return f"{''.join(self.insert_chain)}#{self.handle}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["insert_chain"] = list(self.insert_chain)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EntityRef":
        return cls(
            source=d.get("source", ""),
            handle=str(d.get("handle", "")),
            insert_chain=tuple(d.get("insert_chain") or ()),
            block=d.get("block", ""),
            root_block=d.get("root_block", ""),
            layer=d.get("layer", ""),
            family=d.get("family", ""),
            feature=d.get("feature", ""),
            size_mm=dict(d.get("size_mm") or {}),
            in_overlay=d.get("in_overlay"),
        )


@dataclass
class Finding:
    """One reported defect."""

    kind: str
    severity: str = "WARNING"
    origin: str = "mismatch"
    id: str = ""                                  # "MM-0001", assigned after ranking
    uid: str = ""                                 # sha1 of the two refs + kind
    mech_ref: EntityRef | None = None
    str_ref: EntityRef | None = None
    location: tuple[float, float] = (0.0, 0.0)    # MECH world, drawing units
    str_location: tuple[float, float] | None = None
    offset_mm: tuple[float, float] | None = None   # STR − MECH
    magnitude_mm: float | None = None
    local_offset_mm: tuple[float, float] | None = None
    local_magnitude_mm: float | None = None
    tolerance_mm: float = 0.0
    tolerance_eff_mm: float = 0.0
    ratio: float | None = None
    rotation_deg: float | None = None
    systematic: bool = False
    within_fit_uncertainty: bool = False
    confidence: str = "normal"
    message: str = ""
    details: dict = field(default_factory=dict)

    # ── identity ───────────────────────────────────────────────────
    def compute_uid(self) -> str:
        """``sha1(kind | mech chain#handle | str chain#handle)[:12]``.

        Two runs that find the same defect on the same entities produce
        the same uid regardless of how many other findings there were, so
        ``POST /mismatch/rerun`` can be diffed run against run.
        """
        mech = self.mech_ref.key() if self.mech_ref is not None else "-"
        str_ = self.str_ref.key() if self.str_ref is not None else "-"
        raw = f"{self.kind}|{mech}|{str_}".encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:12]

    # ── convenience ────────────────────────────────────────────────
    def magnitude(self) -> float:
        """``magnitude_mm`` or its Euclidean magnitude from ``offset_mm``."""
        if self.magnitude_mm is not None:
            return float(self.magnitude_mm)
        if self.offset_mm is not None:
            return float((self.offset_mm[0] ** 2 + self.offset_mm[1] ** 2) ** 0.5)
        return 0.0

    def ratio_or_zero(self) -> float:
        r = self.ratio
        return float(r) if r is not None else 0.0


def finding_uid(kind: str, mech_ref: EntityRef | None, str_ref: EntityRef | None) -> str:
    """Free-function form of :meth:`Finding.compute_uid`."""
    mech = mech_ref.key() if mech_ref is not None else "-"
    str_ = str_ref.key() if str_ref is not None else "-"
    return hashlib.sha1(f"{kind}|{mech}|{str_}".encode("utf-8")).hexdigest()[:12]


# ── messages ────────────────────────────────────────────────────────────────

class _SafeDict(dict):
    def __missing__(self, key):                   # pragma: no cover - trivial
        return "{" + key + "}"


def format_message(kind: str, **values) -> str:
    """Render ``MESSAGE_TEMPLATES[kind]``; unknown placeholders survive."""
    tpl = MESSAGE_TEMPLATES.get(kind)
    if tpl is None:
        return values.pop("message", "") or ""
    return tpl.format_map(_SafeDict(values))


# ── deterministic ranking ───────────────────────────────────────────────────

def _ref_handle(ref: EntityRef | None) -> str:
    return "" if ref is None else (ref.handle or "")


def rank_key(f: Finding):
    """The total order of :func:`rank_findings`.

    severity (CRITICAL first) → worst ``ratio`` first (findings without a
    ratio sort *after* every one that has one) → ``kind`` alphabetically
    → ``x`` → ``y`` → MECH handle → STR handle.  ``round(..., 6)`` keeps
    the key from being dominated by sub-nanometre noise, which would make
    the order depend on the last bit of the fit.
    """
    return (
        SEVERITY_RANK.get(f.severity, len(SEVERITIES)),
        1 if f.ratio is None else 0,
        -round(float(f.ratio), 6) if f.ratio is not None else 0.0,
        f.kind or "",
        round(float(f.location[0]), 6),
        round(float(f.location[1]), 6),
        _ref_handle(f.mech_ref),
        _ref_handle(f.str_ref),
    )


def rank_findings(findings, *, assign_ids: bool = True) -> list[Finding]:
    """Sort ``findings`` by :func:`rank_key`, filling ``uid`` and ``id``.

    The sort is stable, so two findings that compare equal keep the order
    the checks emitted them in.  With ``assign_ids`` the ``origin ==
    "mismatch"`` findings are numbered ``MM-0001``, ``MM-0002``, … in rank
    order; ``origin == "spotcheck"`` findings keep the id they were
    promoted with (``ISSUE-…`` / ``MECH-…`` / ``STR-…``) because those
    ids are already referenced by existing reports and UI links.
    """
    ordered = sorted(findings, key=rank_key)
    n = 0
    for f in ordered:
        if not f.uid:
            f.uid = f.compute_uid()
        if assign_ids and f.origin == "mismatch":
            n += 1
            f.id = f"MM-{n:04d}"
    return ordered


def truncate(findings, max_findings) -> tuple[list[Finding], bool]:
    """Keep the first ``max_findings`` (already ranked); ``(kept, truncated)``.

    ``max_findings`` of ``None`` (or a non-positive value) means "keep
    everything" so a policy can switch truncation off.
    """
    items = list(findings)
    if max_findings is None or int(max_findings) <= 0 or len(items) <= int(max_findings):
        return items, False
    return items[: int(max_findings)], True


def summarize(findings) -> dict:
    """``{by_severity, by_kind, by_origin, total}`` with every key present."""
    by_sev = {s: 0 for s in SEVERITIES}
    by_kind = {k: 0 for k in KINDS}
    by_origin = {o: 0 for o in ORIGINS}
    total = 0
    for f in findings:
        total += 1
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
        by_origin[f.origin] = by_origin.get(f.origin, 0) + 1
    by_kind = {k: v for k, v in by_kind.items() if v}
    return {"total": total, "by_severity": by_sev, "by_kind": by_kind, "by_origin": by_origin}


# ── serialisation ───────────────────────────────────────────────────────────

def _pair(v):
    return None if v is None else [float(v[0]), float(v[1])]


def finding_to_dict(f: Finding) -> dict:
    """JSON-safe dict of every field of ``f``."""
    return {
        "id": f.id,
        "uid": f.uid,
        "kind": f.kind,
        "severity": f.severity,
        "origin": f.origin,
        "mech_ref": f.mech_ref.to_dict() if f.mech_ref is not None else None,
        "str_ref": f.str_ref.to_dict() if f.str_ref is not None else None,
        "location": _pair(f.location),
        "str_location": _pair(f.str_location),
        "offset_mm": _pair(f.offset_mm),
        # the same vector, split — the report and the scoreboard read
        # these (A7 asks for dx_mm / dy_mm)
        "dx_mm": (f.offset_mm[0] if f.offset_mm else None),
        "dy_mm": (f.offset_mm[1] if f.offset_mm else None),
        # the location, split — the CSV's shape, so a JSON consumer does
        # not have to unpack "location"
        "x": (f.location[0] if f.location else None),
        "y": (f.location[1] if f.location else None),
        # the grid bay, when the finding knows one (the offset check)
        "grid_bay": (f.details or {}).get("grid_bay"),
        "magnitude_mm": f.magnitude_mm,
        "local_offset_mm": _pair(f.local_offset_mm),
        "local_magnitude_mm": f.local_magnitude_mm,
        "tolerance_mm": f.tolerance_mm,
        "tolerance_eff_mm": f.tolerance_eff_mm,
        "ratio": f.ratio,
        "rotation_deg": f.rotation_deg,
        "systematic": bool(f.systematic),
        "within_fit_uncertainty": bool(f.within_fit_uncertainty),
        "confidence": f.confidence,
        "message": f.message,
        "details": dict(f.details or {}),
    }


def findings_to_dicts(findings) -> list[dict]:
    return [finding_to_dict(f) for f in findings]


def _n(v, nd: int = 6):
    return None if v is None else round(float(v), nd)


def finding_row(f: Finding) -> dict:
    """One ``findings.csv`` row, keyed by :data:`CSV_COLUMNS`.

    ``None`` becomes an empty cell; booleans become ``True``/``False``.
    """
    m, s = f.mech_ref, f.str_ref
    return {
        "id": f.id,
        "uid": f.uid,
        "kind": f.kind,
        "severity": f.severity,
        "origin": f.origin,
        "x": _n(f.location[0]),
        "y": _n(f.location[1]),
        "str_x": _n(f.str_location[0] if f.str_location else None),
        "str_y": _n(f.str_location[1] if f.str_location else None),
        "dx_mm": _n(f.offset_mm[0] if f.offset_mm else None),
        "dy_mm": _n(f.offset_mm[1] if f.offset_mm else None),
        "magnitude_mm": _n(f.magnitude_mm),
        "local_dx_mm": _n(f.local_offset_mm[0] if f.local_offset_mm else None),
        "local_dy_mm": _n(f.local_offset_mm[1] if f.local_offset_mm else None),
        "local_magnitude_mm": _n(f.local_magnitude_mm),
        "tolerance_mm": _n(f.tolerance_mm),
        "tolerance_eff_mm": _n(f.tolerance_eff_mm),
        "ratio": _n(f.ratio),
        "rotation_deg": _n(f.rotation_deg),
        "systematic": bool(f.systematic),
        "within_fit_uncertainty": bool(f.within_fit_uncertainty),
        "confidence": f.confidence,
        "mech_handle": (m.handle if m else None),
        "mech_block": (m.block if m else None),
        "mech_layer": (m.layer if m else None),
        "str_handle": (s.handle if s else None),
        "str_block": (s.block if s else None),
        "str_layer": (s.layer if s else None),
        "message": f.message,
    }


def write_csv(findings, path) -> Path:
    """Write ``findings.csv`` with :data:`CSV_COLUMNS` as the header."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
        w.writeheader()
        for f in findings:
            w.writerow(finding_row(f))
    return p


def write_json(findings, path) -> Path:
    """Write ``findings`` as a bare JSON array (caller wraps it)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(findings_to_dicts(findings), indent=2, default=str),
                 encoding="utf-8")
    return p