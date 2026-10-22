#!/usr/bin/env python3
"""core/clean_v1.py — Stage 4 (LATEST AGGRESSIVE — DEFAULT).

V1 is the recommended default cleaner. It strips all annotation/marking
entities so only real structural geometry remains in the overlay.

See `clean_v2.py` for the previous, less-aggressive version (keeps more
content, useful for comparison or when V1 over-strips something).

Reads:  jobs/<id>/cleaned/<base>.dxf        (raw uploads copied here by stage 3)
Writes: jobs/<id>/cleaned/<base>_clean.dxf
        jobs/<id>/stages/clean_stats.json

Recipe (applied to modelspace AND every block def):
  1. Strip annotation entity types — all DIMENSION subtypes
     (DIMENSION, LARGE_RADIAL_DIMENSION, LINEAR_DIMENSION,
     ALIGNED_DIMENSION, ANGULAR_DIMENSION, ANGULAR3POINT_DIMENSION,
     DIAMETER_DIMENSION, ORDINATE_DIMENSION, ARC_DIMENSION),
     MTEXT, LEADER, MULTILEADER, ATTRIB, ATTDEF, TOLERANCE, DIMASSOC,
     WIPEOUT, OLEFRAME, OLE2FRAME, IMAGE, XLINE, RAY, SHAPE.
  2. Keep TP104-* TEXT entities (grid labels needed by the fit step).
     Strip ALL other TEXT/MTEXT (beam labels, part IDs, sheet text).
  3. Strip LWPOLYLINE hexagons on layer BOX (MECH callout balloons).
  4. Strip non-grid INSERTs:
       MECH: keep A$C*, RTR (real equipment geometry)
       STR:  keep Part-*, Bolt-*, Connection-*, GridLine-*, Line-*
             (real structural geometry)
  5. Always delete INSERTs that point at known-orphan blocks
     (A$C2135de20 etc.) so no dangling INSERTs render ghost geometry.
  6. Strip triangle-shaped entities (3-vertex LWPOLYLINE, SOLID, 3DFACE,
     TRACE) — section marks, revision triangles, hidden-line indicators.
  7. Strip entities on annotation-only layers (DIM, CIVIL, AM_*, REV-*,
     DEFPOINTS, TITLE, BORDER, FRAME).
  8. Wipe ALL entities from paper-space blocks (*Paper_Space*) — sheet
     borders, viewports, scale bars, title blocks, embedded logos.
  9. Drop orphan blocks (anything not referenced anywhere):
       - Always: known-orphan policy.known_orphan_blocks
       - Always: A$C*, Part-, Bolt-, Mark-, StraightDimension-, Text-,
                 Line-, ReferenceModel-, SectionMark-, Unknown-,
                 GridLine-, *U*, *D* (anonymous internal sub-blocks)
       - Never: CAD system blocks (GENAXEH, arrow heads, DIMBLK_*) and
                 blocks referenced by DIMSTYLE table entries.

Mirrors ISGEC/A/scripts/clean.py but with paths parameterized for the
overlay-app job layout. V2 adds aggressive stripping of dimension
subtypes, OLE frames, paper space, annotation layers, and triangles.
"""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf

from .clean_common import (
    ANNOT_TYPES_V1,
    _strip_annotation_layer_entities,
    _strip_annotations_in,
    _strip_marker_hexagons,
    _strip_non_grid_inserts,
    _strip_non_grid_text_in,
    _strip_orphan_blocks_aggressive,
    _strip_orphan_inserts,
    _strip_paper_space,
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

    protected: set of entity handles that must NOT be deleted, even if
        they'd otherwise match a strip rule. Populated by stage 3.5a
        (`core/labels.py`) with the cross-drawing-agreement labels.

    protected_blocks: set of block names that must NOT be deleted even
        if no INSERT references them. Used so that a `GridLine-*` block
        holding a protected label survives orphan-block cleanup.
    """
    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()

    # Step 1a: strip annotation entities in modelspace.
    annot_msp_removed, grid_msp_kept = _strip_annotations_in(
        msp, annot_types=ANNOT_TYPES_V1, policy=policy,
        protected=protected,
    )

    # Step 1b: strip annotation entities INSIDE every non-anonymous block.
    annot_blk_removed = 0
    grid_blk_kept = 0
    for block in list(doc.blocks):
        if block.name.startswith("*"):
            continue
        rm, kp = _strip_annotations_in(block, annot_types=ANNOT_TYPES_V1,
                                       policy=policy, protected=protected)
        annot_blk_removed += rm
        grid_blk_kept += kp

    # Step 1c: wipe ALL entities from paper-space blocks (*Paper_Space*).
    paper_rm = _strip_paper_space(doc, protected=protected)

    # Step 2: strip ALL non-grid-label TEXT in modelspace.
    text_msp_removed, text_msp_kept = _strip_non_grid_text_in(
        msp, policy=policy, protected=protected,
    )

    # Step 2b: strip ALL non-grid-label TEXT inside every non-anonymous block.
    text_blk_removed = 0
    text_blk_kept = 0
    for block in list(doc.blocks):
        if block.name.startswith("*"):
            continue
        rm, kp = _strip_non_grid_text_in(block, policy=policy, protected=protected)
        text_blk_removed += rm
        text_blk_kept += kp

    # Step 3: strip MECH callout balloons.
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

    # Step 4c: strip triangle-shaped entities inside every kept block.
    tri_blk_removed = 0
    for block in list(doc.blocks):
        if block.name.startswith("*"):
            continue
        tri_blk_removed += _strip_triangles_in(block, protected=protected)

    # Step 4d: also strip triangles at modelspace.
    tri_msp_removed = _strip_triangles_in(msp, protected=protected)

    # Step 4e: strip entities on annotation-only layers inside every kept block.
    ann_blk_removed = 0
    for block in list(doc.blocks):
        if block.name.startswith("*"):
            continue
        ann_blk_removed += _strip_annotation_layer_entities(
            block, policy=policy, protected=protected,
        )

    # Step 4f: strip entities on annotation-only layers at modelspace.
    ann_msp_removed = _strip_annotation_layer_entities(
        msp, policy=policy, protected=protected,
    )

    # Step 5: drop orphan blocks (now safe — text + unreferenced INSERTs gone).
    #         protected_blocks keeps block defs alive that hold protected
    #         labels (e.g. `GridLine-*` whose prefix wasn't in top-N).
    blocks_removed = _strip_orphan_blocks_aggressive(
        doc, policy=policy, protected_blocks=protected_blocks,
    )

    # Step 6: save.
    doc.saveas(str(out_path))

    # Verify.
    doc2 = ezdxf.readfile(str(out_path))
    final_types: dict[str, int] = {}
    final_text_total = 0
    for e in doc2.modelspace():
        t = e.dxftype()
        final_types[t] = final_types.get(t, 0) + 1
        if t in ("TEXT", "MTEXT"):
            final_text_total += 1

    return {
        "input": path.name,
        "output": out_path.name,
        "is_str": is_str,
        "annot_removed_modelspace": annot_msp_removed,
        "annot_removed_blocks": annot_blk_removed,
        "paper_space_entities_removed": paper_rm,
        "non_grid_text_removed_modelspace": text_msp_removed,
        "non_grid_text_removed_blocks": text_blk_removed,
        "grid_labels_kept_modelspace": grid_msp_kept + text_msp_kept,
        "grid_labels_kept_blocks": grid_blk_kept + text_blk_kept,
        "hex_markers_removed": hex_removed,
        "non_grid_inserts_removed": inserts_removed,
        "explicit_orphan_inserts_removed": explicit_orphan_inserts,
        "triangles_removed_blocks": tri_blk_removed,
        "triangles_removed_modelspace": tri_msp_removed,
        "annotation_layer_entities_removed_blocks": ann_blk_removed,
        "annotation_layer_entities_removed_modelspace": ann_msp_removed,
        "orphan_blocks_removed": blocks_removed,
        "protected_handles_total": len(protected) if protected else 0,
        "protected_entities_kept": _count_protected_kept(doc2, protected),
        "protected_blocks_total": len(protected_blocks) if protected_blocks else 0,
        "final_modelspace_entities": sum(final_types.values()),
        "final_modelspace_types": final_types,
        "final_text_total": final_text_total,
    }


def _count_protected_kept(doc, protected: set[str] | None) -> int:
    """Count entities in the cleaned DXF whose handle was in `protected`.

    Walks modelspace + every non-anonymous block. Used to surface, in
    `clean_stats.json`, how many protected entities actually survived
    the cleaning pass — a useful signal that the label-first pipeline is
    doing its job.
    """
    if not protected:
        return 0
    n = 0
    for e in doc.modelspace():
        try:
            if e.dxf.handle in protected:
                n += 1
        except Exception:
            pass
    for block in doc.blocks:
        if block.name.startswith("*"):
            continue
        for e in block:
            try:
                if e.dxf.handle in protected:
                    n += 1
            except Exception:
                pass
    return n


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
    # Maps DXF filename -> set of entity handles to skip during stripping.
    # Empty when no agreement was found (or the stage hasn't run yet).
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
        # Look up the protected set for THIS DXF by filename.
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
