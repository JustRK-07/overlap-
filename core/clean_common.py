"""core/clean_common.py — shared helpers for clean_v1 / clean_v2.

Both cleaner versions strip the same kinds of junk (DIMENSION, MTEXT,
hexagons, orphan blocks, etc.) — they only differ in WHICH steps they
run and whether they walk into block definitions. This module holds
the helpers they share.

Project-specific knobs (label regex, block prefixes, marker layer,
annotation layers, known-orphan blocks) live in `CleaningPolicy`
(see core/clean_policy.py). Each helper below accepts a `policy` keyword
defaulting to `DEFAULT_POLICY` — so callers that don't care can ignore
the policy entirely.

Public API:
  Constants:
    ANNOT_TYPES_V1, ANNOT_TYPES_V2      (universal AutoCAD entity-type sets)
  Re-exported from clean_policy:
    DEFAULT_POLICY, CleaningPolicy
  Functions:
    _is_grid_label, _strip_annotations_in, _strip_non_grid_text_in,
    _strip_marker_hexagons, _strip_triangles_in, _strip_orphan_inserts,
    _strip_non_grid_inserts, _strip_orphan_blocks,
    _strip_orphan_blocks_aggressive,
    _strip_annotation_layer_entities, _strip_annotation_layer_text,
    _strip_paper_space
"""

from __future__ import annotations

import re

from .clean_policy import DEFAULT_POLICY, CleaningPolicy

# ─────────────────────────────────────────────────────────────────────────────
# Constants (universal AutoCAD entity types — not project-specific)
# ─────────────────────────────────────────────────────────────────────────────

# Annotation entity types — strip everywhere (modelspace + blocks)
# v1: full list with all DIMENSION subtypes + OLE/WIPEOUT/IMAGE/XLINE/RAY/SHAPE
# v2: subset (no DIMENSION subtypes, no OLE/IMAGE/XLINE)
ANNOT_TYPES_V1 = {
    # Text annotations
    "MTEXT", "ATTRIB", "ATTDEF",
    # Dimensioning (all subtypes — DIMENSION is the parent type, the rest are
    # specialized subtypes like LINEAR/ALIGNED/ANGULAR/RADIAL/DIAMETER/ORDINATE)
    "DIMENSION", "LARGE_RADIAL_DIMENSION", "LINEAR_DIMENSION",
    "ALIGNED_DIMENSION", "ANGULAR_DIMENSION", "ANGULAR3POINT_DIMENSION",
    "DIAMETER_DIMENSION", "ORDINATE_DIMENSION", "ARC_DIMENSION",
    # Leaders + tolerances
    "LEADER", "MULTILEADER", "TOLERANCE",
    # Masking rectangles + legacy embed frames
    "WIPEOUT", "OLEFRAME", "OLE2FRAME",
    # Imagery / reference geometry
    "IMAGE", "XLINE", "RAY", "SHAPE",
    # Dimension association
    "DIMASSOC",
}
ANNOT_TYPES_V2 = {
    "DIMENSION", "MTEXT", "LEADER", "MULTILEADER",
    "ATTRIB", "ATTDEF", "TOLERANCE", "DIMASSOC",
    "WIPEOUT",
}

# Project-specific knobs (label regex, block prefixes, marker layer,
# annotation layers, known-orphan blocks) live in CleaningPolicy
# (see core/clean_policy.py). Each helper here takes an optional
# policy= kwarg defaulting to DEFAULT_POLICY.


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _compile_label_re(policy: CleaningPolicy) -> re.Pattern:
    """Compile the label-regex from a policy. Cached for repeated use."""
    # `re.compile` is cheap but the regex is identical across calls
    # within a single cleaning pass — keep a per-policy cache via
    # function attribute.
    if getattr(_compile_label_re, "_cache_policy", None) is not policy:
        _compile_label_re._cache_pattern = re.compile(policy.label_regex)
        _compile_label_re._cache_policy = policy
    return _compile_label_re._cache_pattern


def _is_protected(entity, protected: set[str] | None) -> bool:
    """True if this entity's handle is in the protected set.

    `protected` is a set of entity handles (uppercase hex strings, ezdxf's
    canonical handle form) that the cleaner must not delete. Used by
    stage 3.5a (`core/labels.py`) to mark cross-drawing-agreement labels
    so the strip helpers below leave them alone — even if they're inside
    a `GridLine-*` block whose prefix isn't in `keep_block_prefixes_str`.
    """
    if not protected:
        return False
    try:
        return entity.dxf.handle in protected
    except Exception:
        return False


def _is_grid_label(entity, *, policy: CleaningPolicy = DEFAULT_POLICY) -> bool:
    """True if this TEXT is a grid label we want to keep."""
    if entity.dxftype() != "TEXT":
        return False
    try:
        return bool(_compile_label_re(policy).match(entity.dxf.text.strip()))
    except Exception:
        return False


def _strip_annotations_in(container, *, annot_types,
                           policy: CleaningPolicy = DEFAULT_POLICY,
                           protected: set[str] | None = None) -> tuple[int, int]:
    """Remove annotation entities from a modelspace OR block. Skip grid labels.

    Returns (removed, kept_grid_labels). Entities whose handles are in
    `protected` are never deleted, regardless of their type — they're
    protected by stage 3.5a (`core/labels.py`).
    """
    removed, kept = 0, 0
    for e in list(container):
        try:
            t = e.dxftype()
            if t in annot_types:
                if _is_protected(e, protected):
                    continue
                # Don't delete grid-label TEXT (matches policy.label_regex)
                if t == "TEXT" and _is_grid_label(e, policy=policy):
                    kept += 1
                    continue
                container.delete_entity(e)
                removed += 1
        except Exception:
            pass
    return removed, kept


def _strip_non_grid_text_in(container,
                            *, policy: CleaningPolicy = DEFAULT_POLICY,
                            protected: set[str] | None = None) -> tuple[int, int]:
    """Aggressively remove ALL TEXT/MTEXT that are NOT grid labels.

    Catches beam labels (TP4BM*, UB20*, ISMC*, ISA*) and any other TEXT
    used as labels/annotations. Grid-label TEXT (matching policy.label_regex)
    is kept. Entities whose handles are in `protected` are never deleted.
    """
    removed, kept = 0, 0
    for e in list(container):
        try:
            if _is_protected(e, protected):
                continue
            t = e.dxftype()
            if t == "TEXT":
                if _is_grid_label(e, policy=policy):
                    kept += 1
                else:
                    container.delete_entity(e)
                    removed += 1
            elif t == "MTEXT":
                # MTEXT is never a grid label
                container.delete_entity(e)
                removed += 1
        except Exception:
            pass
    return removed, kept


def _strip_marker_hexagons(modelspace, *,
                           policy: CleaningPolicy = DEFAULT_POLICY,
                           protected: set[str] | None = None) -> int:
    """Remove 6-vertex closed LWPOLYLINE on the policy marker layer.

    Protected entities are never deleted.
    """
    removed = 0
    for e in list(modelspace):
        try:
            if _is_protected(e, protected):
                continue
            if e.dxftype() != "LWPOLYLINE":
                continue
            if e.dxf.layer != policy.marker_layer:
                continue
            n = len(list(e.vertices()))
            if n == 6:
                modelspace.delete_entity(e)
                removed += 1
        except Exception:
            pass
    return removed


def _strip_triangles_in(container,
                         protected: set[str] | None = None) -> int:
    """Remove triangle-shaped entities (3-vertex LWPOLYLINE, SOLID, 3DFACE,
    TRACE) from a modelspace or block container.

    These are unambiguously annotation markers in structural drawings:
      - Section marks (e.g. "A-A" with hatched triangle)
      - Revision triangles (REV-A, REV-B, REV-C layers)
      - Hidden-line view indicators
      - Detail callout pointers

    Real structural geometry uses ≥ 4 vertices (rectangles, polygons) or
    HATCHES that follow geometry outlines — none of those are affected.

    Protected entities are never deleted.
    """
    removed = 0
    for e in list(container):
        try:
            if _is_protected(e, protected):
                continue
            t = e.dxftype()
            if t in ("SOLID", "3DFACE", "TRACE"):
                container.delete_entity(e)
                removed += 1
            elif t == "LWPOLYLINE":
                v = list(e.vertices())
                if len(v) == 3:
                    container.delete_entity(e)
                    removed += 1
            elif t == "HATCH":
                stripped_any = False
                for path in list(e.paths):
                    try:
                        verts = path.vertices if hasattr(path, "vertices") else []
                        if len(verts) == 3:
                            try:
                                e.paths.remove(path)
                                stripped_any = True
                            except Exception:
                                try:
                                    path.vertices = []
                                    stripped_any = True
                                except Exception:
                                    pass
                    except Exception:
                        pass
                if stripped_any:
                    removed += 1
        except Exception:
            pass
    return removed


def _strip_orphan_inserts(modelspace, *, block_names_to_drop,
                          protected: set[str] | None = None) -> int:
    """Remove INSERT entities in modelspace whose block is in block_names_to_drop.

    Important: must be called BEFORE _strip_orphan_blocks() so the INSERTs
    don't dangle after their block def is deleted. CAD renderers will draw
    a placeholder shape (often a SOLID triangle) for a missing block, which
    is exactly what causes the ghost triangles in the output.

    Protected entities are never deleted.
    """
    removed = 0
    for e in list(modelspace):
        if e.dxftype() != "INSERT":
            continue
        if _is_protected(e, protected):
            continue
        if e.dxf.name in block_names_to_drop:
            modelspace.delete_entity(e)
            removed += 1
    return removed


def _strip_non_grid_inserts(modelspace, *, keep_prefixes,
                            protected: set[str] | None = None) -> int:
    """Remove INSERTs that reference blocks NOT matching a kept prefix.

    STR drawings contain hundreds of Mark-*, Part-*, Bolt-*, Connection-*,
    StraightDimension-*, Text-*, Line-*, ReferenceModel-*, SectionMark-*
    block INSERTs. Only GridLine-* carries the actual gridline geometry;
    everything else is annotation clutter that shouldn't appear in the
    overlay.

    MECH doesn't have this problem (its INSERTs are anonymous A$C* carrying
    equipment geometry), so when called with KEEP_PREFIXES=("A$C", "RTR")
    it's effectively a no-op for MECH.

    Protected entities are never deleted — even if their block name doesn't
    match a kept prefix (e.g. `GridLine-*` whose prefix wasn't in the
    auto-derived top-N but contains a cross-drawing-agreement label).
    """
    removed = 0
    for e in list(modelspace):
        if e.dxftype() != "INSERT":
            continue
        if _is_protected(e, protected):
            continue
        block_name = e.dxf.name
        if not any(block_name.startswith(p) for p in keep_prefixes):
            modelspace.delete_entity(e)
            removed += 1
    return removed


def _strip_orphan_blocks(doc, *,
                         policy: CleaningPolicy = DEFAULT_POLICY,
                         protected_blocks: set[str] | None = None) -> int:
    """Drop blocks that are no longer referenced by anyone.

    Removes:
      - Always: known-orphan policy.known_orphan_blocks
      - A$C* anonymous blocks not referenced anywhere
      - Named-prefix blocks (Part-*, Bolt-*, Mark-*, etc.) not referenced

    IMPORTANT: blocks referenced by DIMSTYLE table entries (arrow heads,
    GENAXEH, etc.) must NEVER be deleted — doing so breaks the dimstyle
    serialization and produces a corrupt DXF.

    `protected_blocks`: any block whose name is in this set is preserved
    regardless of reference count. Used by stage 3.5a (`core/labels.py`)
    to keep `GridLine-*` block defs alive even when no INSERT references
    them any more — because they still hold a cross-drawing-agreement
    TEXT label.
    """
    # 1. Build set of still-referenced block names — from INSERT entities.
    referenced = set()
    for e in doc.modelspace():
        try:
            if e.dxftype() == "INSERT":
                referenced.add(e.dxf.name)
        except Exception:
            pass
    for block in doc.blocks:
        if block.name.startswith("*"):
            continue
        for e in block:
            try:
                if e.dxftype() == "INSERT":
                    referenced.add(e.dxf.name)
            except Exception:
                pass

    # 1b. Also collect blocks referenced by DIMSTYLE table entries.
    #     Arrow blocks, GENAXEH, DIMBLK_*, DIMLDRBLK, DIMTXSTY/BLK etc. must
    #     be preserved even if no INSERT points to them.
    try:
        for dimstyle in doc.dimstyles:
            for attr in ("dimblk", "dimmblk", "dimblk1", "dimblk2",
                         "dimldrblk", "dimltype"):
                try:
                    name = getattr(dimstyle.dxf, attr, None)
                    if name:
                        referenced.add(name)
                except Exception:
                    pass
    except Exception:
        pass

    # 2. Delete unreferenced blocks — be defensive: only delete
    #    named-pattern blocks (A$C*, Part-*, Bolt-*, Mark-*, etc.) and
    #    never blocks with CAD-system names.
    def _is_safe_to_delete(name):
        # Always safe: A$C* anonymous blocks
        if name.startswith("A$C"):
            return True
        # Always safe: known Part-/Bolt-/Mark-/GridLine-/etc. block patterns
        safe_prefixes = (
            "Part-", "Bolt-", "Connection-", "Mark-",
            "StraightDimension-", "Text-", "Line-",
            "ReferenceModel-", "SectionMark-", "Unknown-",
            "GridLine-",
        )
        return any(name.startswith(p) for p in safe_prefixes)

    known_orphans = set(policy.known_orphan_blocks)
    protected = protected_blocks or set()
    removed = 0
    for block in list(doc.blocks):
        name = block.name
        if name.startswith("*"):
            continue
        if name in protected:
            # Stage 3.5a marked this block — leave it intact even if it's
            # no longer referenced anywhere.
            continue
        try:
            if name in known_orphans:
                doc.blocks.delete_block(name, safe=False)
                removed += 1
            elif name not in referenced and _is_safe_to_delete(name):
                doc.blocks.delete_block(name, safe=False)
                removed += 1
        except Exception:
            pass
    return removed


def _strip_orphan_blocks_aggressive(doc, *,
                                    policy: CleaningPolicy = DEFAULT_POLICY,
                                    protected_blocks: set[str] | None = None) -> int:
    """V1 variant of orphan-block removal.

    v1 also deletes *U* and *D* (paper-space / dimension internal
    sub-blocks) when unreferenced, and empty blocks. v2 leaves these
    alone (less aggressive).

    `protected_blocks`: same semantics as `_strip_orphan_blocks` — names
    in this set are preserved regardless of reference count.
    """
    referenced = set()
    for e in doc.modelspace():
        try:
            if e.dxftype() == "INSERT":
                referenced.add(e.dxf.name)
        except Exception:
            pass
    for block in doc.blocks:
        if block.name.startswith("*"):
            continue
        for e in block:
            try:
                if e.dxftype() == "INSERT":
                    referenced.add(e.dxf.name)
            except Exception:
                pass
    try:
        for dimstyle in doc.dimstyles:
            for attr in ("dimblk", "dimmblk", "dimblk1", "dimblk2",
                         "dimldrblk", "dimltype"):
                try:
                    name = getattr(dimstyle.dxf, attr, None)
                    if name:
                        referenced.add(name)
                except Exception:
                    pass
    except Exception:
        pass

    # 1c. Empty blocks (no entities) are never useful — mark for deletion,
    # but never if they're in the protected set.
    protected = protected_blocks or set()
    for block in list(doc.blocks):
        try:
            if any(True for _ in block):
                continue
        except Exception:
            continue
        name = block.name
        if name in protected:
            continue
        if not name.startswith("*") and name not in referenced:
            try:
                doc.blocks.delete_block(name, safe=False)
            except Exception:
                pass

    def _is_safe_to_delete(name):
        if name.startswith("A$C"):
            return True
        safe_prefixes = (
            "Part-", "Bolt-", "Connection-", "Mark-",
            "StraightDimension-", "Text-", "Line-",
            "ReferenceModel-", "SectionMark-", "Unknown-",
            "GridLine-",
        )
        return any(name.startswith(p) for p in safe_prefixes)

    known_orphans = set(policy.known_orphan_blocks)
    removed = 0
    for block in list(doc.blocks):
        name = block.name
        # Skip model-space and paper-space layout blocks — those are
        # essential structural anchors. Delete *U* and *D* (internal
        # sub-blocks of paper space / dimensions) only if not referenced.
        if name.startswith("*"):
            if name in ("*Model_Space", "*Paper_Space", "*Paper_Space0"):
                continue
            if not (name.startswith("*U") or name.startswith("*D")):
                continue
        if name in protected:
            continue
        try:
            if name in known_orphans:
                doc.blocks.delete_block(name, safe=False)
                removed += 1
            elif name not in referenced and _is_safe_to_delete(name):
                doc.blocks.delete_block(name, safe=False)
                removed += 1
        except Exception:
            pass
    return removed


def _strip_annotation_layer_entities(container, *,
                                     policy: CleaningPolicy = DEFAULT_POLICY,
                                     protected: set[str] | None = None) -> int:
    """Strip entities on policy.annotation_layers (DIM, CIVIL, AM_*, REV-*).

    Per AutoCAD layer naming conventions, these layers are always for
    documentation — never real geometry. Affects MECH A$C* blocks mostly.

    Protected entities are never deleted.
    """
    removed = 0
    layers_upper = tuple(l.upper() for l in policy.annotation_layers)
    for e in list(container):
        try:
            if _is_protected(e, protected):
                continue
            layer = (e.dxf.layer or "").upper()
        except Exception:
            continue
        if not layer:
            continue
        for prefix in layers_upper:
            if layer == prefix or layer.startswith(prefix):
                container.delete_entity(e)
                removed += 1
                break
    return removed


def _strip_annotation_layer_text(modelspace, *,
                                policy: CleaningPolicy = DEFAULT_POLICY,
                                protected: set[str] | None = None) -> int:
    """Strip TEXT/MTEXT entities on annotation layers at modelspace only.

    Lighter version of _strip_annotation_layer_entities — only strips
    text content (dimension labels like "R3000"), not geometry. Used by
    the v2 cleaner.

    Protected entities are never deleted.
    """
    removed = 0
    # Subset: only the dimension text layers, not the wide annotation set
    layers_upper = ("DIM", "AM_", "REV-", "CIVIL")
    for e in list(modelspace):
        try:
            if _is_protected(e, protected):
                continue
            if e.dxftype() not in ("TEXT", "MTEXT"):
                continue
            layer = (e.dxf.layer or "").upper()
            if any(layer == p or layer.startswith(p) for p in layers_upper):
                modelspace.delete_entity(e)
                removed += 1
        except Exception:
            pass
    return removed


def _strip_paper_space(doc, protected: set[str] | None = None) -> int:
    """Wipe ALL entities from paper-space blocks (*Paper_Space*).

    Paper space is for layout/viewport/sheet setup — never used when
    rendering the actual structural model. Used by the v1 cleaner.

    Protected entities are never deleted.
    """
    removed = 0
    for block in list(doc.blocks):
        if not block.name.upper().startswith("*PAPER_SPACE"):
            continue
        for e in list(block):
            try:
                if _is_protected(e, protected):
                    continue
                block.delete_entity(e)
                removed += 1
            except Exception:
                pass
    return removed
