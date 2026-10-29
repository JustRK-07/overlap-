#!/usr/bin/env python3
"""tests/synth_perf.py — the performance fixture (PLAN_MISMATCH §6.5).

A drawing pair big enough to show whether the detector scales:

* ``n_side × n_side`` bolt patterns on the MECH side and the matching hole
  patterns on the STR side (71 × 71 = 5 041 patterns, ~20 k bolts and
  ~20 k holes);
* 50 000 MECH noise lines and 100 000 STR noise lines on a layer no
  family matches, so they are walked but never classified;
* 10 seeded defects — a MECH pattern shifted by 8 mm — which must come
  back as exactly 10 ``PATTERN_OFFSET`` findings.

The two drawings are authored in the **same** world coordinates, so the
transform between them is the identity.
"""
from __future__ import annotations

import math
import random
from pathlib import Path

import ezdxf

__all__ = ["build_perf_docs", "PERF_DEFAULTS"]

PERF_DEFAULTS = {
    "n_side": 71,          # 71 x 71 = 5041 patterns
    "pitch": 1500.0,       # mm between pattern centres (>> pattern_eps 400)
    "bolt_r": 10.0,        # MECH bolt radius
    "hole_r": 11.0,        # STR hole radius
    "hole_offset": 100.0,  # the bolts sit at ±offset from the centre
    "n_defects": 10,
    "defect_shift": 8.0,   # mm — the seeded PATTERN_OFFSET
    "mech_noise": 50_000,
    "str_noise": 100_000,
    "noise_len": 200.0,
    "seed": 20261025,
}

MECH_BOLT_LAYER = "BOLT"
STR_HOLE_LAYER = "STEEL-HOLE"
NOISE_LAYER = "NOISE"


def _pattern_centres(n_side: int, pitch: float):
    """The centre of every pattern, in a row-major grid."""
    for row in range(n_side):
        for col in range(n_side):
            yield col * pitch, row * pitch


def build_perf_docs(out_dir: Path, **overrides):
    """Write the two drawings and return ``(mech_path, str_path, truth)``.

    ``truth`` is a dict with the seeded defect positions so the test can
    check that the findings land where it put them.
    """
    cfg = {**PERF_DEFAULTS, **overrides}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(cfg["seed"])
    n_side = cfg["n_side"]
    pitch = cfg["pitch"]
    off = cfg["hole_offset"]
    n_defects = cfg["n_defects"]
    shift = cfg["defect_shift"]

    centres = list(_pattern_centres(n_side, pitch))
    # the seeded defects — spread over the grid, never in the first row
    defect_idx = set()
    if n_defects:
        stride = max(1, len(centres) // (n_defects + 1))
        for k in range(n_defects):
            defect_idx.add(min(len(centres) - 1, (k + 1) * stride))
    defect_truth = []

    # ── the MECH drawing ─────────────────────────────────────────────
    mech = ezdxf.new("R2018")
    mech.header["$INSUNITS"] = 4          # millimetres
    msp = mech.modelspace()
    for layer in (MECH_BOLT_LAYER, NOISE_LAYER):
        mech.layers.add(layer)

    for i, (cx, cy) in enumerate(centres):
        dy = 0.0
        if i in defect_idx:
            dy = shift
            defect_truth.append((cx, cy + dy))
        for dx, ddy in ((-off, -off), (off, -off), (off, off), (-off, off)):
            msp.add_circle((cx + dx, cy + ddy + dy), cfg["bolt_r"],
                           dxfattribs={"layer": MECH_BOLT_LAYER})

    for _ in range(cfg["mech_noise"]):
        x = rng.uniform(0.0, n_side * pitch)
        y = rng.uniform(0.0, n_side * pitch)
        ang = rng.uniform(0.0, 2.0 * math.pi)
        ln = cfg["noise_len"]
        msp.add_line((x, y), (x + ln * math.cos(ang), y + ln * math.sin(ang)),
                     dxfattribs={"layer": NOISE_LAYER})

    mech_path = out_dir / "perf_mech.dxf"
    mech.saveas(str(mech_path))

    # ── the STR drawing (the same world) ─────────────────────────────
    str_doc = ezdxf.new("R2018")
    str_doc.header["$INSUNITS"] = 4
    smsp = str_doc.modelspace()
    for layer in (STR_HOLE_LAYER, NOISE_LAYER):
        str_doc.layers.add(layer)

    for (cx, cy) in centres:
        for dx, ddy in ((-off, -off), (off, -off), (off, off), (-off, off)):
            smsp.add_circle((cx + dx, cy + ddy), cfg["hole_r"],
                            dxfattribs={"layer": STR_HOLE_LAYER})

    for _ in range(cfg["str_noise"]):
        x = rng.uniform(0.0, n_side * pitch)
        y = rng.uniform(0.0, n_side * pitch)
        ang = rng.uniform(0.0, 2.0 * math.pi)
        ln = cfg["noise_len"]
        smsp.add_line((x, y), (x + ln * math.cos(ang), y + ln * math.sin(ang)),
                      dxfattribs={"layer": NOISE_LAYER})

    str_path = out_dir / "perf_str.dxf"
    str_doc.saveas(str(str_path))

    truth = {
        "n_patterns": len(centres),
        "n_defects": len(defect_truth),
        "defect_centres": defect_truth,
        "defect_shift": shift,
        "pitch": pitch,
        "n_entities_mech": len(msp),
        "n_entities_str": len(smsp),
    }
    return mech_path, str_path, truth
