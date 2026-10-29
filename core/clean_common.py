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
    layer_matches, reachable_blocks, _strip_non_grid_attribs,
    _is_grid_label, _strip_annotations_in, _strip_non_grid_text_in,
    _strip_marker_hexagons, _strip_triangles_in, _strip_orphan_inserts,
    _strip_non_grid_inserts, _strip_orphan_blocks,
    _strip_orphan_blocks_aggressive,
    _strip_annotation_layer_entities, _strip_annotation_layer_text,
    _strip_paper_space
"""

from __future__ import annotations

import re
from functools import lru_cache

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

@lru_cache(maxsize=32)
def _compile_label_re(pattern: str) -> re.Pattern:
    """Compile (and cache) a label regex.

    Keyed on the *pattern string*, not on the policy object: a policy whose
    ``label_regex`` is replaced in place must not serve a stale pattern, and
    ``lru_cache`` is thread-safe — the old function-attribute cache could
    hand one concurrent job the other job's regex.
    """
    return re.compile(pattern)


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


_PREFIX_END = ("-", "_", "$", "*")


def layer_matches(layer: str, entry: str) -> bool:
    """Case-insensitive layer match against one policy entry.

    An entry ending in '-', '_', '$' or '*' is a PREFIX (a trailing '*' is
    stripped first); any other entry is an EXACT layer name. So "REV-"
    matches REV-A but "FRAME" matches only a layer named FRAME, never
    FRAME-STEEL.
    """
    layer = (layer or "").upper()
    entry = (entry or "").upper()
    if not layer or not entry:
        return False
    if entry[-1] in _PREFIX_END:
        prefix = entry[:-1] if entry[-1] == "*" else entry
        return bool(prefix) and layer.startswith(prefix)
    return layer == entry


def _layer_in(layer: str, entries) -> bool:
    return any(layer_matches(layer, x) for x in entries)


def _label_text(entity) -> str | None:
    """Text content of a TEXT / MTEXT / ATTRIB entity (None otherwise)."""
    t = entity.dxftype()
    try:
        if t in ("TEXT", "ATTRIB"):
            return entity.dxf.text
        if t == "MTEXT":
            return entity.plain_text()
    except Exception:
        return None
    return None


def _is_grid_label(entity, *, policy: CleaningPolicy = DEFAULT_POLICY) -> bool:
    """True if this TEXT / MTEXT / ATTRIB is a grid label we want to keep."""
    if entity.dxftype() not in ("TEXT", "MTEXT", "ATTRIB"):
        return False
    try:
        txt = _label_text(entity)
        return txt is not None and bool(
            _compile_label_re(policy.label_regex).match(txt.strip()))
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
                if t in ("TEXT", "MTEXT") and _is_grid_label(e, policy=policy):
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
                if _is_grid_label(e, policy=policy):
                    kept += 1
                else:
                    container.delete_entity(e)
                    removed += 1
        except Exception:
            pass
    return removed, kept


def _strip_non_grid_attribs(container,
                            *, policy: CleaningPolicy = DEFAULT_POLICY,
                            protected: set[str] | None = None) -> tuple[int, int]:
    """On every INSERT in `container`, delete ATTRIBs that are not grid labels.

    Returns (removed, kept). ATTRIBs live on the INSERT, not in the
    container, so the per-container annotation strip never sees them.
    Protected ATTRIBs are kept.
    """
    removed, kept = 0, 0
    for ins in list(container):
        try:
            if ins.dxftype() != "INSERT":
                continue
            for att in list(ins.attribs):
                if _is_protected(att, protected) or _is_grid_label(att, policy=policy):
                    kept += 1
                    continue
                ins.delete_attrib(att.dxf.tag)
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


def _is_triangle(e) -> bool:
    t = e.dxftype()
    if t in ("SOLID", "3DFACE", "TRACE"):
        return True
    if t == "LWPOLYLINE":
        return len(list(e.vertices())) == 3
    return False


def _strip_triangles_in(container,
                         protected: set[str] | None = None, *,
                         container_name: str = "",
                         policy: CleaningPolicy = DEFAULT_POLICY) -> int:
    """Remove triangle-shaped MARKER entities from a modelspace or block.

    A SOLID / TRACE / 3DFACE / 3-vertex LWPOLYLINE / 3-vertex HATCH path is
    removed ONLY when it is identified as a marker by context: its layer
    matches policy.triangle_marker_layers or policy.annotation_layers, or
    `container_name` starts with one of policy.marker_block_prefixes.
    Shape alone never triggers removal (gusset plates are triangles).

    Protected entities are never deleted.
    """
    in_marker_block = any(container_name.startswith(p)
                          for p in policy.marker_block_prefixes if p)
    marker_layers = list(policy.triangle_marker_layers) + list(policy.annotation_layers)
    removed = 0
    for e in list(container):
        try:
            if _is_protected(e, protected):
                continue
            if not (in_marker_block or _layer_in(e.dxf.layer, marker_layers)):
                continue
            t = e.dxftype()
            if t == "HATCH":
                stripped_any = False
                # ezdxf 1.4.4: `BoundaryPaths` has no `remove()`; the paths
                # live in its `.paths` list.  A `PolylinePath` carries
                # `.vertices`, an `EdgePath` carries `.edges`.
                from ezdxf.entities.boundary_paths import EdgePath, LineEdge
                for path in list(e.paths):
                    try:
                        if isinstance(path, EdgePath):
                            is_tri = (len(path.edges) == 3
                                      and all(isinstance(ed, LineEdge)
                                              for ed in path.edges))
                        else:
                            is_tri = len(path.vertices) == 3
                        if not is_tri:
                            continue
                        e.paths.paths.remove(path)
                        stripped_any = True
                    except Exception:
                        pass
                if stripped_any:
                    removed += 1
            elif _is_triangle(e):
                container.delete_entity(e)
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
        # Anonymous '*' blocks (arrays, dynamic blocks, *U/*X) carry real
        # geometry and nested INSERTs; they are kept and walked by
        # reachable_blocks.
        if block_name.startswith("*"):
            continue
        if not any(block_name.startswith(p) for p in keep_prefixes):
            modelspace.delete_entity(e)
            removed += 1
    return removed


def reachable_blocks(doc) -> set[str]:
    """Names of all blocks reachable from the drawing.

    BFS roots: modelspace and every paperspace layout, DIMSTYLE block
    attributes, and DIMENSION.dxf.geometry (anonymous *D blocks). Any
    INSERT inside a reachable block -- including '*' anonymous blocks --
    makes its target reachable. Layout blocks are always included.
    """
    reachable: set[str] = set()
    queue: list = []

    def add(name):
        if name and name not in reachable:
            reachable.add(name)
            queue.append(name)

    def scan(container):
        for e in container:
            try:
                t = e.dxftype()
                if t == "INSERT":
                    add(e.dxf.name)
                elif t == "DIMENSION":
                    add(getattr(e.dxf, "geometry", None))
            except Exception:
                pass

    try:
        scan(doc.modelspace())
    except Exception:
        pass
    for layout in doc.layouts:
        try:
            add(layout.block_record_name)
            scan(layout)
        except Exception:
            pass
    for block in doc.blocks:
        n = block.name
        if n.lower() == "*model_space" or n.lower().startswith("*paper_space"):
            add(n)
    try:
        for dimstyle in doc.dimstyles:
            for attr in ("dimblk", "dimblk1", "dimblk2", "dimldrblk", "dimltype"):
                try:
                    add(getattr(dimstyle.dxf, attr, None))
                except Exception:
                    pass
    except Exception:
        pass

    while queue:
        name = queue.pop()
        try:
            block = doc.blocks.get(name)
        except Exception:
            block = None
        if block is not None:
            scan(block)
    return reachable


def _is_layout_block(name: str) -> bool:
    low = name.lower()
    return low == "*model_space" or low.startswith("*paper_space")


_SAFE_PREFIXES = (
    "Part-", "Bolt-", "Connection-", "Mark-",
    "StraightDimension-", "Text-", "Line-",
    "ReferenceModel-", "SectionMark-", "Unknown-",
    "GridLine-",
)


def _is_safe_to_delete(name: str) -> bool:
    return name.startswith("A$C") or any(name.startswith(p) for p in _SAFE_PREFIXES)


def _strip_orphan_blocks(doc, *,
                         policy: CleaningPolicy = DEFAULT_POLICY,
                         protected_blocks: set[str] | None = None) -> int:
    """Drop blocks that are no longer reachable (see `reachable_blocks`).

    Removes known-orphan policy blocks and unreachable A$C* / Part-* /
    Bolt-* / Mark-* ... blocks. Anonymous '*' blocks and layout blocks are
    never deleted here. Blocks reachable only through '*' blocks, paperspace,
    DIMENSION geometry or DIMSTYLE entries are kept.

    `protected_blocks` are preserved regardless (stage 3.5a labels).
    """
    reachable = reachable_blocks(doc)
    known_orphans = set(policy.known_orphan_blocks)
    protected = protected_blocks or set()
    removed = 0
    for block in list(doc.blocks):
        name = block.name
        if name.startswith("*") or name in protected:
            continue
        try:
            if name in known_orphans or (name not in reachable and _is_safe_to_delete(name)):
                doc.blocks.delete_block(name, safe=False)
                removed += 1
        except Exception:
            pass
    return removed


def _strip_orphan_blocks_aggressive(doc, *,
                                    policy: CleaningPolicy = DEFAULT_POLICY,
                                    protected_blocks: set[str] | None = None) -> int:
    """V1 variant: also deletes unreachable *U* / *D* blocks and empty blocks.

    Reachability is computed by `reachable_blocks` (walks into '*' blocks),
    so a block referenced only from an anonymous block is kept.
    """
    reachable = reachable_blocks(doc)
    protected = protected_blocks or set()

    # Empty, unreachable blocks are never useful.
    for block in list(doc.blocks):
        name = block.name
        if name.startswith("*") or name in protected or name in reachable:
            continue
        try:
            if any(True for _ in block):
                continue
            doc.blocks.delete_block(name, safe=False)
        except Exception:
            pass

    known_orphans = set(policy.known_orphan_blocks)
    removed = 0
    for block in list(doc.blocks):
        name = block.name
        if name not in doc.blocks:
            continue
        if name.startswith("*"):
            if _is_layout_block(name):
                continue
            if not (name.startswith("*U") or name.startswith("*D")):
                continue
        if name in protected:
            continue
        try:
            if name in known_orphans or (name not in reachable and _is_safe_to_delete(name)):
                doc.blocks.delete_block(name, safe=False)
                removed += 1
        except Exception:
            pass
    return removed


def _strip_annotation_layer_entities(container, *,
                                     policy: CleaningPolicy = DEFAULT_POLICY,
                                     protected: set[str] | None = None) -> int:
    """Strip entities on policy.annotation_layers (DIM, AM_*, REV-*, ...).

    Per AutoCAD layer naming conventions, these layers are always for
    documentation — never real geometry. Affects MECH A$C* blocks mostly.

    Protected entities are never deleted.
    """
    removed = 0
    for e in list(container):
        try:
            if _is_protected(e, protected):
                continue
            if _layer_in(e.dxf.layer, policy.annotation_layers):
                container.delete_entity(e)
                removed += 1
        except Exception:
            continue
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
    for e in list(modelspace):
        try:
            if _is_protected(e, protected):
                continue
            if e.dxftype() not in ("TEXT", "MTEXT"):
                continue
            if _layer_in(e.dxf.layer, policy.annotation_layers):
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
