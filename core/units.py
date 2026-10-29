#!/usr/bin/env python3
"""core/units.py — explicit drawing units, and what to do when they lie.

Two drawings of one building are only superimposable if you know what a
drawing unit is worth in each file.  The DXF header says (``$INSUNITS``),
but headers are frequently wrong — the TP-104 MECH drawing declares *inch*
and is drawn in *millimetres*.

So the units are never taken on faith.  There are two independent readings:

``header``
    what ``$INSUNITS`` claims.
``grid``
    what the drawings themselves say.  Two drawings of one building have
    the same physical grid spacing, so the ratio of the two grid spacings
    *in drawing units* is the scale factor between them — whatever the
    headers claim.

When the two readings disagree the pipeline **pauses** and asks, offering
the interpretation the geometry supports.  It never rewrites the drawing:
the answer is a per-file override recorded in ``job.json``.

Nothing here ever rescales geometry.  The units only decide what a number
*means*; the transform is still solved from the coordinates as drawn.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────────────────────────
# The units we accept
# ─────────────────────────────────────────────────────────────────────────────

#: The unit names a user may pick.
UNIT_CHOICES = ("mm", "cm", "m", "in", "ft")

#: How many millimetres one drawing unit is worth.
MM_PER_UNIT = {
    "mm": 1.0,
    "cm": 10.0,
    "m": 1000.0,
    "in": 25.4,
    "ft": 304.8,
}

#: The ``$INSUNITS`` code for each unit name (ezdxf's table).
UNIT_CODES = {
    "mm": 4,
    "cm": 5,
    "m": 6,
    "in": 1,
    "ft": 2,
}

#: The inverse of :data:`UNIT_CODES` — only for the units we accept.
CODE_NAMES = {code: name for name, code in UNIT_CODES.items()}

#: Spellings a user (or a header) might use, mapped to our unit names.
_ALIASES = {
    "mm": "mm", "millimeter": "mm", "millimeters": "mm",
    "millimetre": "mm", "millimetres": "mm",
    "cm": "cm", "centimeter": "cm", "centimeters": "cm",
    "centimetre": "cm", "centimetres": "cm",
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "in": "in", "inch": "in", "inches": "in", '"': "in",
    "ft": "ft", "foot": "ft", "feet": "ft", "'": "ft",
}

#: A grid spacing outside this range means the unit reading is nonsense: no
#: building has 127 m between its grid axes, and none has 5 cm either.
#: Used only to *rank* the candidate interpretations, never to reject one.
PLAUSIBLE_SPACING_MM = (1000.0, 100000.0)      # 1 m … 100 m


# ─────────────────────────────────────────────────────────────────────────────
# Primitives
# ─────────────────────────────────────────────────────────────────────────────

def normalise_unit(value) -> str | None:
    """Turn a user-supplied unit into one of :data:`UNIT_CHOICES`.

    Accepts a name (``"mm"``, ``"inches"``), an ``$INSUNITS`` code
    (``4``, ``"4"``) or ``None``.  Returns ``None`` when the value is not a
    unit we support.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        code = int(value)
        return CODE_NAMES.get(code)
    text = str(value).strip().lower()
    if not text:
        return None
    if text in _ALIASES:
        return _ALIASES[text]
    if text.isdigit():
        return CODE_NAMES.get(int(text))
    return None


def mm_per_unit_of(unit) -> float:
    """How many millimetres one drawing unit of ``unit`` is worth.

    ``unit`` may be a name (``"mm"``) or an ``$INSUNITS`` code (``4``).
    An unknown unit is worth **1 mm** — the same conservative assumption
    :func:`core.dxfio.mm_per_unit` makes for a drawing with no units.
    """
    name = normalise_unit(unit)
    if name is not None:
        return MM_PER_UNIT[name]
    if isinstance(unit, (int, float)) and not isinstance(unit, bool):
        return mm_per_unit_of_code(int(unit))
    return 1.0


def mm_per_unit_of_code(code) -> float:
    """How many millimetres one drawing unit of an ``$INSUNITS`` code is worth.

    The five units we offer use the exact table above; anything else
    (µin, mil, yards …) falls back to ezdxf's conversion.  The exact table
    matters: ezdxf's own inch factor is 25.400000000101603, and a
    millimetre offset must survive the round trip.
    """
    name = unit_name(code)
    if name is not None:
        return MM_PER_UNIT[name]
    try:
        from ezdxf.units import conversion_factor
        factor = float(conversion_factor(int(code), 4))       # 4 == mm
    except Exception:
        return 1.0
    if not math.isfinite(factor) or factor <= 0.0:
        return 1.0
    return factor


def unit_code(unit) -> int:
    """The ``$INSUNITS`` code for a unit name, or 0 when unknown."""
    name = normalise_unit(unit)
    return UNIT_CODES.get(name, 0) if name else 0


def unit_name(code) -> str | None:
    """The unit name for an ``$INSUNITS`` code, or ``None`` if unsupported."""
    try:
        return CODE_NAMES.get(int(code))
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# The two readings
# ─────────────────────────────────────────────────────────────────────────────

def grid_scale(mech_spacing: float | None, str_spacing: float | None) -> float | None:
    """The scale between the drawings, from the grids alone.

    ``mech_spacing`` and ``str_spacing`` are median adjacent-axis distances
    in each drawing's **own** units.  Two drawings of one building have the
    same physical spacing, so the ratio of the two numbers is the number of
    MECH units per STR unit — the scale the fit will find.  This reading
    needs no header and is therefore immune to a wrong one.

    ``None`` when either spacing is missing or zero.
    """
    try:
        m = float(mech_spacing)
        s = float(str_spacing)
    except (TypeError, ValueError):
        return None
    if not (m > 0.0) or not (s > 0.0) or not math.isfinite(m) or not math.isfinite(s):
        return None
    return m / s


def header_scale(mech_unit, str_unit) -> float | None:
    """The scale between the drawings, from the two unit readings.

    MECH units per STR unit.  ``None`` when either unit is unknown.
    """
    m = normalise_unit(mech_unit)
    s = normalise_unit(str_unit)
    if not m or not s:
        return None
    return MM_PER_UNIT[s] / MM_PER_UNIT[m]


def physical_spacing_mm(spacing: float | None, unit) -> float | None:
    """A grid spacing expressed in millimetres, given the drawing's unit."""
    try:
        sp = float(spacing)
    except (TypeError, ValueError):
        return None
    if not (sp > 0.0):
        return None
    u = normalise_unit(unit)
    if not u:
        return None
    return sp * MM_PER_UNIT[u]


# ─────────────────────────────────────────────────────────────────────────────
# Suggesting an interpretation
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Suggestion:
    """One interpretation the grid supports."""

    mech: str
    str: str
    spacing_mm: float
    trusted: str                      # "mech" | "str" — whose header we kept
    plausible: bool

    @property
    def same_unit(self) -> bool:
        return self.mech == self.str

    def message(self) -> str:
        if self.same_unit:
            return f"grid spacing indicates both drawings are in {self.mech}"
        return (f"grid spacing indicates the MECH drawing is in {self.mech} "
                f"and the STR drawing is in {self.str}")


def suggest_units(mech_spacing: float | None, str_spacing: float | None,
                  mech_header, str_header) -> Suggestion | None:
    """The interpretation the *grid* supports, given the two headers.

    The grid fixes the **ratio** of the two drawings' units but not the
    unit itself: a 5000-unit grid spacing is 5 m if the drawing is in mm,
    127 m if it is in inches.  So we keep whichever header is plausible and
    derive the other from the grid:

    * trust the STR header → the MECH unit follows from the grid;
    * trust the MECH header → the STR unit follows from the grid.

    Both are then ranked by whether the resulting physical grid spacing is
    a sane distance for a building, and metric wins a tie.  When neither
    header is usable there is nothing to anchor on and ``None`` is returned
    — the caller then reports the conflict without a suggestion.
    """
    g = grid_scale(mech_spacing, str_spacing)
    if g is None:
        return None

    out: list[Suggestion] = []

    # Trust the STR header: mm_per_unit(mech) = mm_per_unit(str) / g.
    s_name = normalise_unit(str_header)
    if s_name:
        want = MM_PER_UNIT[s_name] / g
        for name, factor in MM_PER_UNIT.items():
            if abs(factor - want) <= 1e-9 * max(1.0, factor):
                spacing = physical_spacing_mm(str_spacing, s_name)
                if spacing is None:
                    continue
                out.append(Suggestion(mech=name, str=s_name, spacing_mm=spacing,
                                       trusted="str",
                                       plausible=_plausible(spacing)))
                break

    # Trust the MECH header: mm_per_unit(str) = mm_per_unit(mech) * g.
    m_name = normalise_unit(mech_header)
    if m_name:
        want = MM_PER_UNIT[m_name] * g
        for name, factor in MM_PER_UNIT.items():
            if abs(factor - want) <= 1e-9 * max(1.0, factor):
                spacing = physical_spacing_mm(str_spacing, name)
                if spacing is None:
                    continue
                out.append(Suggestion(mech=m_name, str=name, spacing_mm=spacing,
                                       trusted="mech",
                                       plausible=_plausible(spacing)))
                break

    if not out:
        return None
    # Plausible first; then metric over imperial; then a stable order.
    out.sort(key=lambda s: (not s.plausible,
                             0 if _is_metric(s.str) else 1,
                             0 if _is_metric(s.mech) else 1,
                             s.trusted))
    return out[0]


def _plausible(spacing_mm: float) -> bool:
    lo, hi = PLAUSIBLE_SPACING_MM
    return lo <= spacing_mm <= hi


def _is_metric(name: str) -> bool:
    return name in ("mm", "cm", "m")


# ─────────────────────────────────────────────────────────────────────────────
# The decision
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FileUnits:
    """The units of one file, and where that answer came from."""

    file_id: str = ""
    role: str = "other"
    name: str = "mm"
    header_code: int | None = None
    header_name: str | None = None
    override: str | None = None
    source: str = "header"          # "header" | "override"
    reason: str = ""

    @property
    def code(self) -> int:
        return UNIT_CODES.get(self.name, 4)

    @property
    def known(self) -> bool:
        """Do we actually know this drawing's units?

        True when the header said so, or when the user set them.  False for
        a unitless drawing with no override — its units are *assumed*, and a
        prior built on an assumption is no prior at all.
        """
        return bool(self.header_name or self.override)

    @property
    def mm_per_unit(self) -> float:
        return MM_PER_UNIT.get(self.name, 1.0)

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "role": self.role,
            "units": self.name,
            "header_insunits": self.header_code,
            "header_units": self.header_name,
            "override": self.override,
            "source": self.source,
            "reason": self.reason,
            "mm_per_unit": self.mm_per_unit,
        }


@dataclass
class Decision:
    """The units of the pair, and whether the readings agreed."""

    mech: FileUnits = field(default_factory=FileUnits)
    str: FileUnits = field(default_factory=FileUnits)
    grid_scale: float | None = None
    header_scale: float | None = None
    conflict: bool = False
    message: str = ""
    suggestion: Suggestion | None = None

    @property
    def agree(self) -> bool:
        return not self.conflict

    def to_dict(self) -> dict:
        return {
            "mech": self.mech.to_dict(),
            "str": self.str.to_dict(),
            "grid_scale": self.grid_scale,
            "header_scale": self.header_scale,
            "conflict": self.conflict,
            "message": self.message,
            "suggestion": ({"mech": self.suggestion.mech,
                            "str": self.suggestion.str,
                            "message": self.suggestion.message(),
                            "trusted": self.suggestion.trusted,
                            "spacing_mm": self.suggestion.spacing_mm}
                           if self.suggestion else None),
            "choices": list(UNIT_CHOICES),
        }


def decide(mech: FileUnits, str_: FileUnits, *,
           mech_spacing: float | None = None,
           str_spacing: float | None = None,
           tolerance: float = 0.02) -> Decision:
    """Compare the grid reading with the header reading and decide.

    The comparison is between the scale the **grid** implies and the scale
    the **chosen units** imply.  An override that matches the grid is not a
    conflict — the user has already answered the question, and the answer
    is checked against the geometry.
    """
    g = grid_scale(mech_spacing, str_spacing)
    h = header_scale(mech.name, str_.name)
    dec = Decision(mech=mech, str=str_, grid_scale=g, header_scale=h)

    if g is None or h is None:
        dec.conflict = False
        dec.message = (
            "the unit headers could not be checked against the grid "
            "(no usable spacing or unit) — the units as given are used")
        return dec

    rel = abs(g / h - 1.0)
    if rel <= tolerance:
        dec.conflict = False
        dec.message = (
            f"the grid spacing and the unit headers agree "
            f"(scale {g:.6g} vs {h:.6g})")
        return dec

    dec.conflict = True
    dec.suggestion = suggest_units(mech_spacing, str_spacing,
                                   mech.header_name or mech.name,
                                   str_.header_name or str_.name)
    head = (f"the grid spacing says the two drawings share a scale of "
            f"{g:.6g}, but the units say {h:.6g} "
            f"({rel:.0%} apart)")
    if dec.suggestion is not None:
        dec.message = (
            f"{head} — {dec.suggestion.message()}. "
            "Choose the units for each drawing to continue.")
    else:
        dec.message = (
            f"{head}. Choose the units for each drawing to continue.")
    return dec
