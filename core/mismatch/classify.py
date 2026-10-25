#!/usr/bin/env python3
"""core/mismatch/classify.py — family assignment for extracted features.

PLAN_MISMATCH §1.2.  A feature belongs to family *F* when

1. ``F.src`` is the drawing the feature came from, **and**
2. the feature's type is in ``F.feature``, **and**
3. ``F.layer_regex`` matches the feature's *effective* layer (display
   prefixes already stripped) **or** ``F.block_regex`` matches any block
   name in the feature's INSERT chain, **and**
4. the feature's world-mapped size in mm is inside ``F``'s range.

A feature may satisfy several families at once — that is deliberate: a
rect on ``EQUIP-SKID`` is both ``mech_skid`` and ``mech_equipment``, and
an anchor circle drawn on ``ANCHOR-BOLT`` is both ``str_anchor`` and
``str_hole``.  The *checks* decide which pairing applies; classification
only says "these families recognise this shape".

The **layer prefilter** is the performance half of this module: the layer
and block regexes are the cheapest test and reject the overwhelming
majority of entities in a real drawing, so :meth:`Classifier.candidates`
memoises "which families could possibly match *this* (layer, block chain)"
in a plain dict.  One lookup tells ``features.py`` to skip an entity
entirely, without touching its geometry.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from .policy import DEFAULT_POLICY, Family, MismatchPolicy

__all__ = [
    "Classifier",
    "normalize_layer",
    "LAYER_PREFIXES",
]

#: Display prefixes the overlay writes in front of a layer name.  They carry
#: no meaning for matching and are stripped before a regex ever sees the
#: layer (PLAN_MISMATCH §1.1).
LAYER_PREFIXES = ("STR$0$", "MECH|", "STR|")

_PREFIX_RE = re.compile(r"^(?:STR\$0\$|MECH\||STR\|)")


def normalize_layer(name) -> str:
    """Strip a ``MECH|`` / ``STR|`` / ``STR$0$`` display prefix.

    Idempotent, and only ever strips from the front — ``STEEL-HOLE`` and
    ``STR|STEEL-HOLE`` both come out as ``STEEL-HOLE``.
    """
    return _PREFIX_RE.sub("", str(name or ""))


class Classifier:
    """Compiled view of a :class:`~core.mismatch.policy.MismatchPolicy`."""

    __slots__ = ("policy", "_by_src", "_rules", "_cand_cache", "_srcs")

    def __init__(self, policy: MismatchPolicy | None = None) -> None:
        self.policy = policy if policy is not None else DEFAULT_POLICY
        fams = tuple(self.policy.families)
        self._rules: tuple[tuple[Family, object, object], ...] = tuple(
            (f, f.layer_re(), f.block_re()) for f in fams)
        self._by_src: dict[str, tuple[Family, ...]] = {}
        for src in ("MECH", "STR"):
            self._by_src[src] = tuple(f for f in fams if f.src == src)
        self._srcs = tuple(sorted(self._by_src))
        self._cand_cache: dict[tuple[str, tuple[str, ...]], frozenset] = {}

    # ── lookups ────────────────────────────────────────────────────
    def sources(self) -> tuple[str, ...]:
        return self._srcs

    def families(self, source: str, feature_type: str | None = None) -> tuple[Family, ...]:
        """Families declared for ``source``, optionally filtered by type."""
        fams = self._by_src.get(source, ())
        if feature_type is None:
            return fams
        return tuple(f for f in fams if f.has_feature(feature_type))

    def feature_types(self, source: str) -> tuple[str, ...]:
        """Every feature type any family of ``source`` accepts."""
        out: list[str] = []
        for f in self._by_src.get(source, ()):
            for t in f.features:
                if t not in out:
                    out.append(t)
        return tuple(out)

    def families_naming(self, source: str, feature_type: str) -> tuple[Family, ...]:
        return self.families(source, feature_type)

    def band_families(self, source: str) -> tuple[Family, ...]:
        """Families of ``source`` whose feature type is ``band`` (members)."""
        return self.families(source, "band")

    def point_families(self, source: str) -> tuple[Family, ...]:
        """Families that accept a bare point or an INSERT (§1.1 Point)."""
        return tuple(f for f in self._by_src.get(source, ())
                     if f.has_feature("point") or f.has_feature("insert"))

    def has_point_families(self, source: str) -> bool:
        return bool(self.point_families(source))

    def has_band_families(self, source: str) -> bool:
        return bool(self.band_families(source))

    def band_material(self, source: str, layer, blocks: Iterable[str] | None = None) -> bool:
        """Could geometry on this layer / in this chain become part of a band?

        §1.1 builds bands out of *segments*, but no family declares the
        ``segment`` feature type — a member is a **band**, made of two
        edges.  A segment therefore has to survive extraction even though
        it belongs to no family on its own, otherwise there would be
        nothing left to pair.  This is the one place that exception is
        made, and only for layers/blocks a band family recognises.
        """
        fams = self.band_families(source)
        if not fams:
            return False
        cands = self.candidates(layer, blocks)
        return any(f.name in cands for f in fams)

    def family(self, name: str) -> Family | None:
        return self.policy.family(name)

    # ── the prefilter ──────────────────────────────────────────────
    def candidates(self, layer, blocks: Iterable[str] | None = None) -> frozenset:
        """Family names whose layer **or** block regex matches — memoised.

        ``layer`` is matched case-insensitively after
        :func:`normalize_layer`; every name in ``blocks`` (the INSERT
        chain, modelspace first) is tried against each block regex.  An
        empty result means *nothing in the policy can ever claim this
        entity* and the caller should skip it without reading geometry.
        """
        key = (normalize_layer(layer), tuple(blocks or ()))
        hit = self._cand_cache.get(key)
        if hit is None:
            ln = key[0]
            names = []
            for fam, lre, bre in self._rules:
                if lre is not None and lre.search(ln):
                    names.append(fam.name)
                elif bre is not None:
                    for b in key[1]:
                        if bre.search(b):
                            names.append(fam.name)
                            break
            hit = frozenset(names)
            self._cand_cache[key] = hit
        return hit

    def prefilter_cache_size(self) -> int:
        return len(self._cand_cache)

    # ── the actual assignment ──────────────────────────────────────
    def assign(self, *, source: str, feature_type: str, size_mm: dict,
               layer, blocks: Iterable[str] | None = None,
               candidates: frozenset | None = None) -> tuple[str, ...]:
        """Every family of ``source`` that claims this feature, in policy order."""
        cands = self.candidates(layer, blocks) if candidates is None else candidates
        if not cands:
            return ()
        ln = normalize_layer(layer)
        blks = tuple(blocks or ())
        out: list[str] = []
        for fam, lre, bre in self._rules:
            if fam.src != source or not fam.has_feature(feature_type):
                continue
            if fam.name not in cands:
                continue
            if lre is not None and lre.search(ln):
                pass
            elif bre is not None and any(bre.search(b) for b in blks):
                pass
            else:
                continue
            if not fam.size_ok(size_mm or {}):
                continue
            out.append(fam.name)
        return tuple(out)

    def is_point_candidate(self, layer, blocks: Iterable[str] | None = None) -> bool:
        """Does any point family recognise this layer / block chain?"""
        cands = self.candidates(layer, blocks)
        if not cands:
            return False
        for fam, _lre, _bre in self._rules:
            if fam.name in cands and (fam.has_feature("point") or fam.has_feature("insert")):
                return True
        return False

    # ── diagnostics ────────────────────────────────────────────────
    def describe(self) -> dict:
        """A JSON-safe dump of the classification rules (for mismatch.json)."""
        return {f.name: f.to_dict() for f in self.policy.families}

    def coverage_hints(self) -> dict[str, Sequence[str]]:
        """``{family: (layer_regex, block_regex)}`` for the UI banner."""
        return {f.name: (f.layer_regex or "", f.block_regex or "")
                for f in self.policy.families}