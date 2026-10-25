#!/usr/bin/env python3
"""core/mismatch/policy.py — the mismatch policy: schema, defaults, lookup.

Everything project-specific about mismatch detection lives in one JSON
document (PLAN_MISMATCH §1.5): which layers and blocks make up a family,
which families check against which, the mm tolerances, the search radii,
the band geometry and the local/systematic context window.

Two properties matter more than anything else here:

**Strictness.**  :meth:`MismatchPolicy.from_dict` rejects any key it does
not know, at the top level *and* inside every nested section.  A typo in
``"tol_mm": {"suport": 10}`` must fail loudly, not silently leave the
default in place — the whole point of a policy file is that what you read
is what ran.

**Precedence.**  Four sources, each overriding the one before it, merged
one *top-level section* at a time:

1. :data:`DEFAULT_POLICY` (the §1.2 family table + the §1.5 JSON)
2. ``jobs/<job_id>/mismatch_policy.json``
3. ``<pair_root>/mismatch_policy.json``
4. ``job.options["mismatch"]``

A source that only sets ``checks`` leaves every other section at whatever
the previous source had.  The result is identified by
:func:`policy_hash`, which lands in ``stages/mismatch.json`` so a run can
be traced back to the exact configuration that produced it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

__all__ = [
    "PolicyError",
    "Family",
    "Pairs",
    "Checks",
    "Tol",
    "Search",
    "Band",
    "Context",
    "Render",
    "MismatchPolicy",
    "DEFAULT_POLICY",
    "DEFAULT_FAMILIES",
    "POLICY_FILENAME",
    "policy_hash",
    "load_policy",
    "resolve_policy_path",
]

#: File name looked for in ``jobs/<id>/`` and in ``<pair_root>/``.
POLICY_FILENAME = "mismatch_policy.json"

SOURCES = ("MECH", "STR")
FEATURES = ("circle", "rect", "polygon", "band", "point", "insert", "pattern")
SIZE_KEYS = ("r", "w", "h", "len", "wh")
RESTRICT_TO = ("all", "grid_bbox")
EMIT_SYSTEMATIC = ("info", "drop", "warning")


class PolicyError(ValueError):
    """A policy document is malformed: unknown key, wrong type, bad regex."""


# ── tiny strict-JSON helpers ────────────────────────────────────────────────

def _obj(d, where: str) -> dict:
    if not isinstance(d, dict):
        raise PolicyError(f"{where} must be a JSON object, got {type(d).__name__}")
    return d


def _strict(d: dict, allowed, where: str) -> None:
    extra = sorted(set(d) - set(allowed))
    if extra:
        raise PolicyError(
            f"unknown key(s) in {where}: {', '.join(repr(k) for k in extra)}; "
            f"allowed keys are {', '.join(sorted(allowed))}")


def _need(d: dict, key: str, where: str):
    if key not in d:
        raise PolicyError(f"{where} is missing required key {key!r}")
    return d[key]


def _num(d: dict, key: str, where: str, *, lo: float | None = None,
         hi: float | None = None) -> float:
    v = _need(d, key, where)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise PolicyError(f"{where}.{key} must be a number, got {v!r}")
    v = float(v)
    if not (v == v and abs(v) != float("inf")):
        raise PolicyError(f"{where}.{key} must be finite, got {v!r}")
    if lo is not None and v < lo:
        raise PolicyError(f"{where}.{key} must be >= {lo}, got {v}")
    if hi is not None and v > hi:
        raise PolicyError(f"{where}.{key} must be <= {hi}, got {v}")
    return v


def _opt_num(d: dict, key: str, where: str, *, lo=None, hi=None):
    if key not in d or d[key] is None:
        return None
    return _num(d, key, where, lo=lo, hi=hi)


def _int(d: dict, key: str, where: str, *, lo=None, hi=None) -> int:
    v = _num(d, key, where, lo=lo, hi=hi)
    if v != int(v):
        raise PolicyError(f"{where}.{key} must be a whole number, got {v}")
    return int(v)


def _bool(d: dict, key: str, where: str) -> bool:
    v = _need(d, key, where)
    if not isinstance(v, bool):
        raise PolicyError(f"{where}.{key} must be true or false, got {v!r}")
    return v


def _enum(d: dict, key: str, where: str, allowed) -> str:
    v = _need(d, key, where)
    if v not in allowed:
        raise PolicyError(f"{where}.{key} must be one of {list(allowed)}, got {v!r}")
    return v


def _regex(pattern, where: str) -> str | None:
    """Validate a regex eagerly; ``None`` means "this dimension is unused"."""
    if pattern is None:
        return None
    if not isinstance(pattern, str):
        raise PolicyError(f"{where} must be a string or null, got {pattern!r}")
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise PolicyError(f"{where}: invalid regular expression {pattern!r} ({exc})") from exc
    return pattern


def _pair_list(value, where: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, (list, tuple)):
        raise PolicyError(f"{where} must be a list of [mech_family, str_family] pairs")
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise PolicyError(f"{where}[{i}] must be a two-element [mech, str] list")
        a, b = item
        if not isinstance(a, str) or not isinstance(b, str):
            raise PolicyError(f"{where}[{i}] family names must be strings")
        out.append((a, b))
    return tuple(out)


# ── sections ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Family:
    """One recognised class of item (§1.2).

    A feature belongs to this family when its ``src`` and feature type
    match, its world-mapped size in mm is inside the range, **and** either
    ``layer_regex`` matches the effective layer or ``block_regex`` matches
    any block name in the INSERT chain.  A feature may belong to several
    families.

    ``size_key`` says which dimension ``size_min_mm``/``size_max_mm``
    apply to: ``"r"`` (radius), ``"w"``, ``"h"``, ``"len"`` (a band's
    length) or ``"wh"`` (both w and h must be in range).
    """

    name: str
    src: str
    features: tuple[str, ...]
    layer_regex: str | None
    block_regex: str | None
    size_min_mm: float | None = None
    size_max_mm: float | None = None
    size_key: str = "w"

    KEYS = ("src", "feature", "layer_regex", "block_regex",
            "size_min_mm", "size_max_mm", "size_key")

    def __post_init__(self) -> None:
        if self.src not in SOURCES:
            raise PolicyError(f"family {self.name!r}: src must be one of {list(SOURCES)}, "
                              f"got {self.src!r}")
        if not self.features:
            raise PolicyError(f"family {self.name!r}: feature must name at least one type")
        bad = [f for f in self.features if f not in FEATURES]
        if bad:
            raise PolicyError(f"family {self.name!r}: unknown feature type(s) {bad}; "
                              f"allowed {list(FEATURES)}")
        if self.layer_regex is None and self.block_regex is None:
            raise PolicyError(f"family {self.name!r}: at least one of "
                              "layer_regex / block_regex is required")
        if self.size_key not in SIZE_KEYS:
            raise PolicyError(f"family {self.name!r}: size_key must be one of "
                              f"{list(SIZE_KEYS)}, got {self.size_key!r}")

    def has_feature(self, feature: str) -> bool:
        return feature in self.features

    def layer_re(self):
        """Compiled layer regex, or ``None``."""
        return None if self.layer_regex is None else re.compile(self.layer_regex, re.IGNORECASE)

    def block_re(self):
        """Compiled block-name regex, or ``None``."""
        return None if self.block_regex is None else re.compile(self.block_regex, re.IGNORECASE)

    def size_ok(self, size_mm: dict) -> bool:
        """Is a feature's world-mapped ``{w, h, r, len}`` inside the range?"""
        if self.size_key == "wh":
            keys = ("w", "h")
        else:
            keys = (self.size_key,)
        for k in keys:
            v = size_mm.get(k)
            if v is None:
                return False
            if self.size_min_mm is not None and v < self.size_min_mm:
                return False
            if self.size_max_mm is not None and v > self.size_max_mm:
                return False
        return True

    def to_dict(self) -> dict:
        return {"src": self.src,
                "feature": list(self.features),
                "layer_regex": self.layer_regex,
                "block_regex": self.block_regex,
                "size_min_mm": self.size_min_mm,
                "size_max_mm": self.size_max_mm,
                "size_key": self.size_key}

    @classmethod
    def from_dict(cls, name: str, d, where: str | None = None) -> "Family":
        w = where or f"families[{name!r}]"
        d = _obj(d, w)
        _strict(d, cls.KEYS, w)
        feat = _need(d, "feature", w)
        if isinstance(feat, str):
            feat = (feat,)
        return cls(
            name=name,
            src=_enum(d, "src", w, SOURCES),
            features=tuple(feat),
            layer_regex=_regex(d.get("layer_regex"), f"{w}.layer_regex"),
            block_regex=_regex(d.get("block_regex"), f"{w}.block_regex"),
            size_min_mm=_opt_num(d, "size_min_mm", w, lo=0.0),
            size_max_mm=_opt_num(d, "size_max_mm", w, lo=0.0),
            size_key=d.get("size_key", "w"),
        )


@dataclass(frozen=True)
class Pairs:
    """Which family checks against which (§1.2, bottom)."""

    bolts: tuple[tuple[str, str], ...] = (("mech_bolt", "str_hole"),
                                           ("mech_plate_hole", "str_anchor"))
    plate_fallback: str = "str_plate"
    support: tuple[tuple[str, str], ...] = (("mech_support", "str_member"),
                                             ("mech_support_pt", "str_member"))
    containment: tuple[tuple[str, str], ...] = (("mech_skid", "str_outline"),)
    clash: tuple[tuple[str, str], ...] = (("mech_equipment", "str_column"),)

    KEYS = ("bolts", "plate_fallback", "support", "containment", "clash")

    def to_dict(self) -> dict:
        return {"bolts": [list(p) for p in self.bolts],
                "plate_fallback": self.plate_fallback,
                "support": [list(p) for p in self.support],
                "containment": [list(p) for p in self.containment],
                "clash": [list(p) for p in self.clash]}

    @classmethod
    def from_dict(cls, d, where: str = "pairs") -> "Pairs":
        d = _obj(d, where)
        _strict(d, cls.KEYS, where)
        kw = {}
        for k in ("bolts", "support", "containment", "clash"):
            if k in d:
                kw[k] = _pair_list(d[k], f"{where}.{k}")
        if "plate_fallback" in d:
            v = d["plate_fallback"]
            if not isinstance(v, str):
                raise PolicyError(f"{where}.plate_fallback must be a family name")
            kw["plate_fallback"] = v
        return cls(**kw)


@dataclass(frozen=True)
class Checks:
    """Which checks run at all."""

    bolts: bool = True
    support: bool = True
    containment: bool = True
    clash: bool = True
    extra_hole: bool = False          # off by default: a hole with no bolt is normal

    KEYS = ("bolts", "support", "containment", "clash", "extra_hole")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.KEYS}

    @classmethod
    def from_dict(cls, d, where: str = "checks") -> "Checks":
        d = _obj(d, where)
        _strict(d, cls.KEYS, where)
        return cls(**{k: _bool(d, k, where) for k in cls.KEYS if k in d})


@dataclass(frozen=True)
class Tol:
    """Millimetre tolerances. ``min_clearance_mm`` 0 disables the clearance check."""

    concentric_abs: float = 0.5
    concentric_default: float = 2.0
    bolt_edge_min: float = 20.0
    support: float = 10.0
    point: float = 5.0
    outline: float = 5.0
    min_edge: float = 50.0
    clash: float = 1.0
    min_clearance: float = 0.0

    KEYS = ("concentric_abs", "concentric_default", "bolt_edge_min", "support",
            "point", "outline", "min_edge", "clash", "min_clearance")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.KEYS}

    @classmethod
    def from_dict(cls, d, where: str = "tol_mm") -> "Tol":
        d = _obj(d, where)
        _strict(d, cls.KEYS, where)
        return cls(**{k: _num(d, k, where, lo=0.0) for k in cls.KEYS if k in d})


@dataclass(frozen=True)
class Search:
    """Search radii in mm; each is capped at a fraction of the grid spacing."""

    bolt: float = 150.0
    pattern_eps: float = 400.0
    member: float = 1000.0
    outline: float = 1000.0

    KEYS = ("bolt", "pattern_eps", "member", "outline")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.KEYS}

    @classmethod
    def from_dict(cls, d, where: str = "search_mm") -> "Search":
        d = _obj(d, where)
        _strict(d, cls.KEYS, where)
        return cls(**{k: _num(d, k, where, lo=0.0) for k in cls.KEYS if k in d})


@dataclass(frozen=True)
class Band:
    """How a beam is turned into a plan-view band (§1.1 b/c)."""

    min_mm: float = 50.0
    max_mm: float = 600.0
    default_flange_mm: float = 200.0
    min_member_len_mm: float = 500.0

    KEYS = ("min_mm", "max_mm", "default_flange_mm", "min_member_len_mm")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.KEYS}

    @classmethod
    def from_dict(cls, d, where: str = "band") -> "Band":
        d = _obj(d, where)
        _strict(d, cls.KEYS, where)
        out = {k: _num(d, k, where, lo=0.0) for k in cls.KEYS if k in d}
        if "min_mm" in out and "max_mm" in out and out["min_mm"] > out["max_mm"]:
            raise PolicyError(f"{where}: min_mm ({out['min_mm']}) > max_mm ({out['max_mm']})")
        return cls(**out)


@dataclass(frozen=True)
class Context:
    """Local-vs-systematic analysis window (§3)."""

    k_neighbours: int = 8
    radius_mm: float = 5000.0
    min_neighbours: int = 3
    k_fit: float = 3.0
    systematic_min_items: int = 5
    systematic_min_fraction: float = 0.3
    emit_systematic_items: str = "info"

    KEYS = ("k_neighbours", "radius_mm", "min_neighbours", "k_fit",
            "systematic_min_items", "systematic_min_fraction", "emit_systematic_items")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.KEYS}

    @classmethod
    def from_dict(cls, d, where: str = "context") -> "Context":
        d = _obj(d, where)
        _strict(d, cls.KEYS, where)
        kw = {}
        for k in ("k_neighbours", "min_neighbours", "systematic_min_items"):
            if k in d:
                kw[k] = _int(d, k, where, lo=0)
        for k in ("radius_mm", "k_fit"):
            if k in d:
                kw[k] = _num(d, k, where, lo=0.0)
        if "systematic_min_fraction" in d:
            kw["systematic_min_fraction"] = _num(
                d, "systematic_min_fraction", where, lo=0.0, hi=1.0)
        if "emit_systematic_items" in d:
            kw["emit_systematic_items"] = _enum(
                d, "emit_systematic_items", where, EMIT_SYSTEMATIC)
        return cls(**kw)


@dataclass(frozen=True)
class Render:
    """Marker styling for the QA|MISMATCH_* layers (§4)."""

    marker_radius_mm: float | str = "auto"
    draw_info: bool = False
    draw_refs: bool = True

    KEYS = ("marker_radius_mm", "draw_info", "draw_refs")

    def radius_mm(self, default: float) -> float:
        """The marker radius in mm, resolving ``"auto"`` to ``default``."""
        if isinstance(self.marker_radius_mm, str):
            return float(default)
        return float(self.marker_radius_mm)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.KEYS}

    @classmethod
    def from_dict(cls, d, where: str = "render") -> "Render":
        d = _obj(d, where)
        _strict(d, cls.KEYS, where)
        kw = {}
        if "marker_radius_mm" in d:
            v = d["marker_radius_mm"]
            if isinstance(v, str):
                if v != "auto":
                    raise PolicyError(
                        f"{where}.marker_radius_mm must be a number or \"auto\", got {v!r}")
                kw["marker_radius_mm"] = v
            else:
                kw["marker_radius_mm"] = _num(d, "marker_radius_mm", where, lo=0.0)
        for k in ("draw_info", "draw_refs"):
            if k in d:
                kw[k] = _bool(d, k, where)
        return cls(**kw)


# ── the policy ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MismatchPolicy:
    """The whole configuration of one mismatch run."""

    enabled: bool = True
    source_docs: str = "converted"
    families: tuple[Family, ...] = ()
    pairs: Pairs = field(default_factory=Pairs)
    checks: Checks = field(default_factory=Checks)
    tol: Tol = field(default_factory=Tol)
    search: Search = field(default_factory=Search)
    band: Band = field(default_factory=Band)
    crit_bearing: float = 0.5
    context: Context = field(default_factory=Context)
    detect_polygon_circles: bool = True
    reconstruct_rects_from_lines: bool = True
    restrict_to: str = "all"
    grid_margin_bays: int = 1
    render: Render = field(default_factory=Render)
    max_findings: int = 5000
    # Knobs §1.3 refers to but §1.5's JSON does not spell out; promoted to
    # top level so they are strict-checked and hashed like everything else.
    clash_with_members: bool = False        # beams excluded from clash unless True
    containment_requires_counterpart: bool = False

    KEYS = ("enabled", "source_docs", "families", "pairs", "checks", "tol_mm",
            "search_mm", "band", "crit_bearing", "context",
            "detect_polygon_circles", "reconstruct_rects_from_lines",
            "restrict_to", "grid_margin_bays", "render", "max_findings",
            "clash_with_members", "containment_requires_counterpart")

    SECTIONS = ("pairs", "checks", "tol_mm", "search_mm", "band", "context", "render")

    # ── derived lookups ────────────────────────────────────────────
    def family(self, name: str) -> Family | None:
        for f in self.families:
            if f.name == name:
                return f
        return None

    def family_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.families)

    def require_family(self, name: str) -> Family:
        f = self.family(name)
        if f is None:
            raise PolicyError(f"policy has no family {name!r}")
        return f

    # ── serialisation ──────────────────────────────────────────────
    def to_dict(self) -> dict:
        """The canonical JSON form; ``from_dict(to_dict()) == self``."""
        return {
            "enabled": self.enabled,
            "source_docs": self.source_docs,
            "families": {f.name: f.to_dict() for f in self.families},
            "pairs": self.pairs.to_dict(),
            "checks": self.checks.to_dict(),
            "tol_mm": self.tol.to_dict(),
            "search_mm": self.search.to_dict(),
            "band": self.band.to_dict(),
            "crit_bearing": self.crit_bearing,
            "context": self.context.to_dict(),
            "detect_polygon_circles": self.detect_polygon_circles,
            "reconstruct_rects_from_lines": self.reconstruct_rects_from_lines,
            "restrict_to": self.restrict_to,
            "grid_margin_bays": self.grid_margin_bays,
            "render": self.render.to_dict(),
            "max_findings": self.max_findings,
            "clash_with_members": self.clash_with_members,
            "containment_requires_counterpart": self.containment_requires_counterpart,
        }

    @classmethod
    def from_dict(cls, d, where: str = "policy") -> "MismatchPolicy":
        """Parse a policy document; **any** unknown key raises PolicyError."""
        d = _obj(d, where)
        _strict(d, cls.KEYS, where)

        fam_raw = _obj(_need(d, "families", where), f"{where}.families")
        families = tuple(Family.from_dict(name, spec, f"{where}.families[{name!r}]")
                         for name, spec in fam_raw.items())

        kw: dict = {"families": families}
        if "enabled" in d:
            kw["enabled"] = _bool(d, "enabled", where)
        if "source_docs" in d:
            kw["source_docs"] = _enum(d, "source_docs", where, ("converted", "cleaned"))
        if "pairs" in d:
            kw["pairs"] = Pairs.from_dict(d["pairs"], f"{where}.pairs")
        if "checks" in d:
            kw["checks"] = Checks.from_dict(d["checks"], f"{where}.checks")
        if "tol_mm" in d:
            kw["tol"] = Tol.from_dict(d["tol_mm"], f"{where}.tol_mm")
        if "search_mm" in d:
            kw["search"] = Search.from_dict(d["search_mm"], f"{where}.search_mm")
        if "band" in d:
            kw["band"] = Band.from_dict(d["band"], f"{where}.band")
        if "crit_bearing" in d:
            kw["crit_bearing"] = _num(d, "crit_bearing", where, lo=0.0, hi=1.0)
        if "context" in d:
            kw["context"] = Context.from_dict(d["context"], f"{where}.context")
        if "detect_polygon_circles" in d:
            kw["detect_polygon_circles"] = _bool(d, "detect_polygon_circles", where)
        if "reconstruct_rects_from_lines" in d:
            kw["reconstruct_rects_from_lines"] = _bool(d, "reconstruct_rects_from_lines", where)
        if "restrict_to" in d:
            kw["restrict_to"] = _enum(d, "restrict_to", where, RESTRICT_TO)
        if "grid_margin_bays" in d:
            kw["grid_margin_bays"] = _int(d, "grid_margin_bays", where, lo=0)
        if "render" in d:
            kw["render"] = Render.from_dict(d["render"], f"{where}.render")
        if "max_findings" in d:
            kw["max_findings"] = _int(d, "max_findings", where, lo=0)
        if "clash_with_members" in d:
            kw["clash_with_members"] = _bool(d, "clash_with_members", where)
        if "containment_requires_counterpart" in d:
            kw["containment_requires_counterpart"] = _bool(
                d, "containment_requires_counterpart", where)

        pol = cls(**kw)
        pol._validate_references()
        return pol

    def _validate_references(self) -> None:
        """Every family named by ``pairs`` / ``plate_fallback`` must exist."""
        known = set(self.family_names())
        refs: list[str] = [self.pairs.plate_fallback]
        for group in (self.pairs.bolts, self.pairs.support,
                      self.pairs.containment, self.pairs.clash):
            for a, b in group:
                refs += [a, b]
        for name in refs:
            if name and name not in known:
                raise PolicyError(
                    f"pairs reference unknown family {name!r}; defined families are "
                    f"{sorted(known)}")

    # ── convenience ────────────────────────────────────────────────
    def replace(self, **kw) -> "MismatchPolicy":
        """``dataclasses.replace`` that re-validates family references."""
        pol = replace(self, **kw)
        pol._validate_references()
        return pol

    @property
    def policy_hash(self) -> str:
        """16 hex chars of sha256 over the canonical JSON; order-independent."""
        return policy_hash(self)


# ── defaults (§1.2 family table) ────────────────────────────────────────────

DEFAULT_FAMILIES: tuple[Family, ...] = (
    Family("mech_bolt", "MECH", ("circle",),
           r"bolt|nut|fastener|stud", r"bolt|nut|stud|flange", 3.0, 40.0, "r"),
    Family("mech_plate_hole", "MECH", ("circle",),
           r"baseplate|base.?plate|hole", r"baseplate|base.?plate", 3.0, 45.0, "r"),
    Family("mech_support", "MECH", ("rect",),
           r"saddle|support|shoe|foot|base.?plate", r"saddle|shoe|^SUP", 50.0, 5000.0, "wh"),
    Family("mech_support_pt", "MECH", ("point", "insert"),
           r"pipe.?supp|^ps\b|support.?point", r"^PS-|^PS_", None, None, "w"),
    Family("mech_equipment", "MECH", ("rect", "polygon"),
           r"equip|main object|vessel|pump|tank", None, 200.0, 30000.0, "w"),
    Family("mech_skid", "MECH", ("rect", "polygon"),
           r"skid", r"skid", 300.0, 30000.0, "w"),
    Family("str_hole", "STR", ("circle",),
           r"hole|bolt|plate", r"hole|bolt|plate", 3.0, 45.0, "r"),
    Family("str_anchor", "STR", ("circle",),
           r"anchor", r"anchor", 3.0, 45.0, "r"),
    Family("str_plate", "STR", ("rect", "polygon"),
           r"plate", r"plate", 50.0, 3000.0, "w"),
    Family("str_member", "STR", ("band",),
           r"frame|beam|member|girder|joist", r"beam", 500.0, None, "len"),
    Family("str_column", "STR", ("rect", "polygon"),
           r"\bcol|column|post", r"col", 100.0, 3000.0, "w"),
    Family("str_outline", "STR", ("rect", "polygon"),
           r"civil|found|plinth|pedestal", r"plinth|found", 300.0, None, "w"),
)

#: The baseline policy: the §1.2 table plus the §1.5 JSON verbatim.
DEFAULT_POLICY = MismatchPolicy(families=DEFAULT_FAMILIES)


# ── hashing ─────────────────────────────────────────────────────────────────

def policy_hash(policy: MismatchPolicy | dict) -> str:
    """A stable fingerprint of a policy: sha256 of its canonical JSON.

    ``sort_keys=True`` means the hash depends on *content* only, never on
    the key order a policy file happened to use, so a project can reformat
    its JSON without invalidating every cached run.
    """
    d = policy.to_dict() if isinstance(policy, MismatchPolicy) else policy
    blob = json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ── lookup / precedence ─────────────────────────────────────────────────────

def resolve_policy_path(job_id=None, pair_root=None) -> list[Path]:
    """The two on-disk policy paths, highest precedence first."""
    out: list[Path] = []
    if job_id:
        out.append(Path(_job_dir(job_id)) / POLICY_FILENAME)
    if pair_root:
        out.append(Path(pair_root) / POLICY_FILENAME)
    return out


def _job_dir(job_id) -> Path:
    from jobs import job_dir                    # late: tests monkeypatch JOBS_DIR
    return job_dir(str(job_id))


def _job_options(job_id):
    if not job_id:
        return None
    try:
        from jobs import load_job
        job = load_job(str(job_id))
    except Exception:                            # unreadable/absent job is not fatal
        return None
    return getattr(job, "options", None) if job is not None else None


def _read_policy_file(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PolicyError(f"{path} is not valid JSON: {exc}") from exc


def load_policy(job_id=None, pair_root=None, *, options=None,
                defaults: MismatchPolicy | None = None) -> MismatchPolicy:
    """Resolve the effective policy for one run.

    Precedence, each overriding the previous, merged a *top-level section*
    at a time (so a file that only sets ``checks`` keeps the default
    families):

    ``DEFAULT_POLICY``
        → ``jobs/<job_id>/mismatch_policy.json``
        → ``<pair_root>/mismatch_policy.json``
        → ``job.options["mismatch"]``

    ``options`` overrides the job file lookup (used by tests and by the
    API); ``defaults`` replaces the bottom layer.
    """
    base = dict((defaults or DEFAULT_POLICY).to_dict())
    sources: list[tuple[str, object]] = []
    for p in resolve_policy_path(job_id, pair_root):
        sources.append((str(p), _read_policy_file(p)))
    opts = options if options is not None else _job_options(job_id)
    if opts:
        sources.append(("job.options['mismatch']", opts.get("mismatch")))

    for name, doc in sources:
        if doc is None:
            continue
        if not isinstance(doc, dict):
            raise PolicyError(f"{name} must be a JSON object, got {type(doc).__name__}")
        base.update(doc)
    return MismatchPolicy.from_dict(base)