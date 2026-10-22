#!/usr/bin/env python3
"""core/clean_v2.py — Stage 4 (PREVIOUS / LESS-AGGRESSIVE — ALTERNATIVE).

V2 is the previous version of the cleaner. Available as an alternative
in the UI for comparison or when V1 over-strips something you need.

Recipe (applied to BOTH modelspace and every non-anonymous block def):
  1. Strip annotation entities (DIMENSION, MTEXT, LEADER, MULTILEADER,
     ATTRIB, ATTDEF, TOLERANCE, DIMASSOC, WIPEOUT) at modelspace level.
     Skip entities inside block defs.
  2. Strip ALL TEXT/MTEXT entities at modelspace EXCEPT those matching
     the grid-label regex (^TP104-[A-D1-3]$).
  3. Strip LWPOLYLINE hexagons on layer BOX.
  4. Strip orphan A$C* anonymous blocks (referenced by zero entities).
  5. Strip orphan A$C2135de20 ACM_FILLED_HALF symbol block.

The differences from V1:
  - V2 only scans modelspace for annotations; V1 also strips annotations
    inside every block def (beam labels inside Mark-* / Part-* sub-blocks).
  - V2 only strips MTEXT-style text; V1 also strips TEXT/MTEXT inside
    every block def.
  - V2 keeps the original anonymous A$C* blocks intact; V1 strips them
    aggressively when unreferenced.

For most projects V1 gives a much cleaner result. V2 is kept for cases
where V1's aggressive stripping removes geometry the user actually needs.

See `clean_v1.py` for the recommended default.
"""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf

from .clean_common import (
    ANNOT_TYPES_V2,
    _strip_annotations_in,
    _strip_annotation_layer_text,
    _strip_marker_hexagons,
    _strip_non_grid_inserts,
    _strip_non_grid_text_in,
    _strip_orphan_blocks,
    _strip_orphan_inserts,
    _strip_triangles_in,
)
from .clean_policy import DEFAULT_POLICY
from jobs import job_dir


def _clean_one(path: Path, out_path: Path, *, is_str: bool,
               policy=DEFAULT_POLICY,
               protected: set[str] | None = None,
               protected_blocks: set[str] | None = None) -> dict:
    """Clean a single DXF. Returns stats dict.

    is_str: True for STR drawings (aggressive INSERT filter — only the
            prefixes in policy.keep_block_prefixes_str are kept). False
            for MECH (keep prefixes in policy.keep_block_prefixes_mech,
            which historically includes anonymous A$C* + RTR holding real
            equipment geometry like HATCH + LWPOLYLINE).

    protected: set of entity handles that must NOT be deleted (populated
        by stage 3.5a from cross-drawing-agreement labels).

    protected_blocks: set of block names that must NOT be deleted even
        if no INSERT references them.
    """
    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()

    # Step 1: strip annotation entities (DIMENSION, MTEXT, etc.) in modelspace.
    annot_msp_removed, grid_msp_kept = _strip_annotations_in(
        msp, annot_types=ANNOT_TYPES_V2, policy=policy,
        protected=protected,
    )

    # Step 1b: strip annotation entities INSIDE every non-anonymous block.
    annot_blk_removed = 0
    for block in list(doc.blocks):
        if block.name.startswith("*"):
            continue
        rm, _ = _strip_annotations_in(
            block, annot_types=ANNOT_TYPES_V2, policy=policy,
            protected=protected,
        )
        annot_blk_removed += rm

    # Step 2: strip ALL non-grid-label TEXT in modelspace.
    text_msp_removed, text_msp_kept = _strip_non_grid_text_in(
        msp, policy=policy, protected=protected,
    )

    # Step 2b: strip ALL non-grid-label TEXT inside every non-anonymous block.
    text_blk_removed = 0
    for block in list(doc.blocks):
        if block.name.startswith("*"):
            continue
        rm, _ = _strip_non_grid_text_in(
            block, policy=policy, protected=protected,
        )
        text_blk_removed += rm

    # Step 2c: strip TEXT/MTEXT on annotation layers at modelspace.
    annot_text_msp = _strip_annotation_layer_text(
        msp, policy=policy, protected=protected,
    )

    # Step 2d: strip TEXT/MTEXT on annotation layers INSIDE every non-anonymous block.
    annot_text_blk = 0
    for block in list(doc.blocks):
        if block.name.startswith("*"):
            continue
        annot_text_blk += _strip_annotation_layer_text(
            block, policy=policy, protected=protected,
        )

    # Step 3: strip MECH callout balloons
    hex_removed = _strip_marker_hexagons(msp, policy=policy, protected=protected)

    # Step 4: drop INSERTs that reference non-grid blocks.
    keep_prefixes = (policy.keep_block_prefixes_str if is_str
                     else policy.keep_block_prefixes_mech)
    inserts_removed = _strip_non_grid_inserts(
        msp, keep_prefixes=keep_prefixes, protected=protected,
    )

    # Step 4b: ALWAYS remove INSERTs that point at known-orphan blocks.
    explicit_orphan_inserts = _strip_orphan_inserts(
        msp, block_names_to_drop=policy.known_orphan_blocks,
        protected=protected,
    )

    # Step 4c+d: strip triangle-shaped entities at modelspace + blocks.
    tri_msp_removed = _strip_triangles_in(msp, protected=protected)
    tri_blk_removed = 0
    for block in list(doc.blocks):
        if block.name.startswith("*"):
            continue
        tri_blk_removed += _strip_triangles_in(block, protected=protected)

    # Step 5: drop orphan blocks (now safe — text + unreferenced INSERTs gone)
    blocks_removed = _strip_orphan_blocks(
        doc, policy=policy, protected_blocks=protected_blocks,
    )

    # Step 6: save
    doc.saveas(str(out_path))

    # Verify
    doc2 = ezdxf.readfile(str(out_path))
    final_types: dict[str, int] = {}
    final_text_total = 0
    for e in doc2.modelspace():
        t = e.dxftype()
        final_types[t] = final_types.get(t, 0) + 1
        if t in ("TEXT", "MTEXT"):
            final_text_total += 1

    # Count protected entities that survived.
    prot_kept = 0
    if protected:
        for e in doc2.modelspace():
            try:
                if e.dxf.handle in protected:
                    prot_kept += 1
            except Exception:
                pass
        for block in doc2.blocks:
            if block.name.startswith("*"):
                continue
            for e in block:
                try:
                    if e.dxf.handle in protected:
                        prot_kept += 1
                except Exception:
                    pass

    return {
        "input": path.name,
        "output": out_path.name,
        "is_str": is_str,
        "annot_removed_modelspace": annot_msp_removed,
        "annot_removed_blocks": annot_blk_removed,
        "non_grid_text_removed_modelspace": text_msp_removed,
        "non_grid_text_removed_blocks": text_blk_removed,
        "annotation_layer_text_removed_modelspace": annot_text_msp,
        "annotation_layer_text_removed_blocks": annot_text_blk,
        "grid_labels_kept_modelspace": grid_msp_kept + text_msp_kept,
        "hex_markers_removed": hex_removed,
        "non_grid_inserts_removed": inserts_removed,
        "explicit_orphan_inserts_removed": explicit_orphan_inserts,
        "triangles_removed_blocks": tri_blk_removed,
        "triangles_removed_modelspace": tri_msp_removed,
        "orphan_blocks_removed": blocks_removed,
        "protected_handles_total": len(protected) if protected else 0,
        "protected_entities_kept": prot_kept,
        "protected_blocks_total": len(protected_blocks) if protected_blocks else 0,
        "final_modelspace_entities": sum(final_types.values()),
        "final_modelspace_types": final_types,
        "final_text_total": final_text_total,
    }


def run(job_id: str) -> None:
    d = job_dir(job_id)
    cleaned = d / "cleaned"
    stages = d / "stages"
    stages.mkdir(exist_ok=True)

    # Load the policy derived by stage 3.5b (clean_policy). If missing
    # (legacy job / no stage 3.5b yet), fall back to DEFAULT_POLICY.
    policy_path = stages / "clean_policy.json"
    if policy_path.exists():
        from .clean_policy import CleaningPolicy
        try:
            policy = CleaningPolicy.from_dict(json.loads(policy_path.read_text()))
        except Exception:
            policy = DEFAULT_POLICY
    else:
        policy = DEFAULT_POLICY

    # Load the protected-handle list produced by stage 3.5a (labels.py).
    protected_map: dict[str, set[str]] = {}
    protected_blocks_map: dict[str, set[str]] = {}
    handles_path = stages / "protected_handles.json"
    if handles_path.exists():
        try:
            raw = json.loads(handles_path.read_text())
            for fname, handles in raw.items():
                protected_map[fname] = {str(h) for h in handles}
        except Exception:
            protected_map = {}
    blocks_path = stages / "protected_blocks.json"
    if blocks_path.exists():
        try:
            raw = json.loads(blocks_path.read_text())
            for fname, blocks in raw.items():
                protected_blocks_map[fname] = {str(b) for b in blocks}
        except Exception:
            protected_blocks_map = {}

    stats = []
    for dxf in sorted(cleaned.glob("*.dxf")):
        if dxf.stem.endswith("_clean"):
            continue
        out_dxf = cleaned / f"{dxf.stem}_clean.dxf"
        is_str = "STR" in dxf.stem.upper()
        prot = protected_map.get(dxf.name) or set()
        prot_blocks = protected_blocks_map.get(dxf.name) or set()
        try:
            stats.append(_clean_one(
                dxf, out_dxf, is_str=is_str, policy=policy,
                protected=prot or None,
                protected_blocks=prot_blocks or None,
            ))
        except Exception as e:
            stats.append({"input": dxf.name, "error": str(e)})

    (stages / "clean_stats.json").write_text(json.dumps(stats, indent=2))
