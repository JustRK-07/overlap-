#!/usr/bin/env python3
"""core/overlay.py — Stage 7.

Applies the fitted affine to the STR drawing, producing the final
overlay DXF. Supports 3 strategies:

  A (per-entity)    preserve ORIGINAL colors
  B (2-block wrap)  MECH→red, STR→blue,  _BLUE_ prefix
  C (1-block wrap)  MECH→red, STR→blue,  _STR_  prefix

Reads:
  cleaned/<base>_clean.dxf  (MECH)
  cleaned/<base>_clean.dxf  (STR)
  transform.json             (fitted affine)

Writes:
  out/superimposed.dxf       (~18 MB)
"""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
from ezdxf.addons.importer import Importer

from jobs import job_dir, load_job

# ─────────────────────────────────────────────────────────────────────────────
# Color policy
# ─────────────────────────────────────────────────────────────────────────────

MECH_COLOR = 1   # red  (ACI 1)
STR_COLOR = 5    # blue (ACI 5)

# ─────────────────────────────────────────────────────────────────────────────
# Helper: affine-transform an entity in place
# ─────────────────────────────────────────────────────────────────────────────

def _transform_xy(x, y, ax, ay, bx, by):
    return (ax * x + bx, ay * y + by)


def _block_lookup(doc):
    """Return a fresh {name: block} dict for `doc.blocks`.

    `doc.blocks` (and `doc.blocks.__contains__` / `.get`) uses an
    internal index that does NOT refresh after `block_record.rename()`
    — so freshly renamed blocks are findable by iteration but not by
    name lookup. This helper rebuilds the lookup map every call so
    downstream code can find renamed blocks reliably.
    """
    return {b.name: b for b in doc.blocks}


def _transform_vertex(v, ax, ay, bx, by):
    """Transform a vertex (2- or 3-tuple) preserving the 3rd element (bulge or z)."""
    if len(v) >= 3:
        return (ax * v[0] + bx, ay * v[1] + by, v[2])
    return (ax * v[0] + bx, ay * v[1] + by)


def _prescale_entity(entity, sx, sy, ip_x=0.0, ip_y=0.0):
    """Bake an INSERT's pos + xscale into each entity in a block.

    Converts block-local coords → STR-world coords by replicating
    INSERT.pos + xscale * local.
    """
    if sx == 1.0 and sy == 1.0 and ip_x == 0.0 and ip_y == 0.0:
        return
    t = entity.dxftype()
    try:
        if t in ("LINE", "RAY", "XLINE"):
            entity.dxf.start = (ip_x + sx * entity.dxf.start[0],
                                ip_y + sy * entity.dxf.start[1], 0.0)
            entity.dxf.end = (ip_x + sx * entity.dxf.end[0],
                              ip_y + sy * entity.dxf.end[1], 0.0)
        elif t == "LWPOLYLINE":
            entity.vertices = [
                (ip_x + sx * v[0], ip_y + sy * v[1], v[2] if len(v) > 2 else 0.0)
                for v in entity.vertices()
            ]
        elif t in ("CIRCLE", "ARC"):
            entity.dxf.center = (ip_x + sx * entity.dxf.center[0],
                                 ip_y + sy * entity.dxf.center[1], 0.0)
            entity.dxf.radius = entity.dxf.radius * max(abs(sx), abs(sy))
        elif t == "ELLIPSE":
            entity.dxf.center = (ip_x + sx * entity.dxf.center[0],
                                 ip_y + sy * entity.dxf.center[1], 0.0)
        elif t == "POINT":
            entity.dxf.location = (ip_x + sx * entity.dxf.location[0],
                                   ip_y + sy * entity.dxf.location[1], 0.0)
        elif t in ("TEXT", "MTEXT"):
            entity.dxf.insert = (ip_x + sx * entity.dxf.insert[0],
                                 ip_y + sy * entity.dxf.insert[1], 0.0)
        elif t == "INSERT":
            entity.dxf.insert = (ip_x + sx * entity.dxf.insert[0],
                                 ip_y + sy * entity.dxf.insert[1], 0.0)
        elif t == "SPLINE":
            try:
                entity.control_points = [
                    (ip_x + sx * p[0], ip_y + sy * p[1], p[2] if len(p) > 2 else 0.0)
                    for p in entity.control_points
                ]
            except Exception:
                pass
        elif t == "HATCH":
            for path in entity.paths:
                try:
                    path.vertices = [
                        (ip_x + sx * v[0], ip_y + sy * v[1], v[2] if len(v) > 2 else 0.0)
                        for v in path.vertices
                    ]
                except Exception:
                    pass
        elif t == "DIMENSION":
            entity.dxf.insert = (ip_x + sx * entity.dxf.insert[0],
                                 ip_y + sy * entity.dxf.insert[1], 0.0)
        elif t in ("SOLID", "TRACE", "3DFACE"):
            for attr in ("vtx0", "vtx1", "vtx2", "vtx3",
                         "first_corner", "second_corner",
                         "third_corner", "fourth_corner"):
                try:
                    v = getattr(entity.dxf, attr)
                    if len(v) >= 3:
                        setattr(entity.dxf, attr,
                                (ip_x + sx * v[0], ip_y + sy * v[1], v[2]))
                    else:
                        setattr(entity.dxf, attr,
                                (ip_x + sx * v[0], ip_y + sy * v[1]))
                except Exception:
                    pass
    except Exception:
        pass


def _transform_entity_inplace(entity, ax, ay, bx, by):
    """Apply the affine (a, b) to an entity's geometry in place."""
    t = entity.dxftype()
    try:
        if t in ("LINE", "RAY", "XLINE"):
            entity.dxf.start = _transform_xy(entity.dxf.start[0], entity.dxf.start[1], ax, ay, bx, by)
            entity.dxf.end = _transform_xy(entity.dxf.end[0], entity.dxf.end[1], ax, ay, bx, by)
        elif t == "LWPOLYLINE":
            new_pts = [_transform_vertex(v, ax, ay, bx, by) for v in entity.vertices()]
            entity.clear()
            for p in new_pts:
                entity.append_vertices(p)
        elif t in ("CIRCLE", "ARC"):
            entity.dxf.center = _transform_xy(entity.dxf.center[0], entity.dxf.center[1], ax, ay, bx, by)
            entity.dxf.radius = entity.dxf.radius * max(abs(ax), abs(ay))
        elif t == "ELLIPSE":
            entity.dxf.center = _transform_xy(entity.dxf.center[0], entity.dxf.center[1], ax, ay, bx, by)
        elif t == "POINT":
            entity.dxf.location = _transform_xy(entity.dxf.location[0], entity.dxf.location[1], ax, ay, bx, by)
        elif t in ("TEXT", "MTEXT"):
            entity.dxf.insert = _transform_xy(entity.dxf.insert[0], entity.dxf.insert[1], ax, ay, bx, by)
        elif t == "INSERT":
            pass  # handled elsewhere
        elif t == "SPLINE":
            try:
                entity.control_points = [
                    _transform_vertex(p, ax, ay, bx, by)
                    for p in entity.control_points
                ]
            except Exception:
                pass
        elif t == "HATCH":
            for path in entity.paths:
                try:
                    path.vertices = [
                        _transform_vertex(v, ax, ay, bx, by)
                        for v in path.vertices
                    ]
                except Exception:
                    pass
        elif t == "DIMENSION":
            entity.dxf.insert = _transform_xy(entity.dxf.insert[0], entity.dxf.insert[1], ax, ay, bx, by)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Strategy A — per-entity, preserve original colors
# ─────────────────────────────────────────────────────────────────────────────

def _strategy_A(mech_doc, str_doc_obj, ax, ay, bx, by):
    """Imports every STR entity into MECH's modelspace, applies the affine.
    Original colors preserved."""
    STR_PREFIX = "_STR_"

    # Step 1: import all STR blocks (renamed with _STR_ prefix)
    imp = Importer(str_doc_obj, mech_doc)
    block_map = {}
    for str_block in list(str_doc_obj.blocks):
        if str_block.name.startswith("*"):
            continue
        target = STR_PREFIX + str_block.name
        if target in _block_lookup(mech_doc):
            try:
                mech_doc.blocks.delete_block(target, safe=False)
            except Exception:
                pass
        try:
            new_name = imp.import_block(str_block.name, rename=False)
            if new_name != target:
                # BlockLayout.rename doesn't exist in ezdxf 1.4.x — must
                # rename the underlying block_record, which propagates to
                # the layout. The except: keep the original name and let
                # the importer's collision suffix do its job.
                name_to_block = _block_lookup(mech_doc)
                blk = name_to_block.get(new_name)
                if blk is not None:
                    blk.block_record.rename(target)
                    new_name = target
        except Exception:
            new_name = str_block.name
        block_map[str_block.name] = new_name
    try:
        imp.finalize()
    except Exception:
        pass

    # Step 2: import STR non-INSERT entities via the Importer, then
    # clone+rename INSERTs manually. The Importer copies INSERTs but
    # leaves their block references pointing at the original (un-
    # prefixed) STR block names, so every STR INSERT ends up dangling.
    # We import everything via the Importer first, then strip the
    # un-rewritten INSERTs and re-add them with the renamed names.
    mech_handles = {e.dxf.handle for e in mech_doc.modelspace()
                    if e.dxf.hasattr("handle")}
    for entity in list(str_doc_obj.modelspace()):
        try:
            imp.import_entity(entity, target_layout=mech_doc.modelspace())
        except Exception:
            pass
    try:
        imp.finalize()
    except Exception:
        pass

    # Rewrite INSERT names IN-PLACE: the Importer copied the STR INSERTs
    # with their original (un-prefixed) block names, leaving dangling
    # references in modelspace. Rather than drop+clone (which is fragile
    # for INSERTs whose block names contain spaces / dashes, common in
    # `GridLine-...` blocks), we just rewrite `dxf.name` on each
    # Importer-imported INSERT. This preserves every attribute of the
    # INSERT (handle, xscale, yscale, layers, transforms) and keeps it
    # properly linked to the renamed _STR_* block.
    #
    # Critically, this must also walk INTO every block def — when
    # STR's geometry included nested INSERTs (e.g., a block that
    # contains INSERTs referencing other blocks), the Importer copied
    # them as-is too. Without rewrites here, those nested INSERTs
    # remain dangling in the block defs, and ezdxf's Frontend raises
    # DXFStructureError on the first one it encounters, aborting the
    # whole preview render.
    #
    # We rebuild `known_blocks` and `block_map` lookups fresh each call
    # because `block_record.rename()` updates the block_record without
    # refreshing the `doc.blocks` index.
    name_to_block = _block_lookup(mech_doc)
    known_blocks = set(name_to_block.keys())

    def _rewrite_insert_names_in(layout):
        for e in list(layout):
            try:
                if e.dxftype() != "INSERT":
                    continue
                cur = e.dxf.name
                if cur in known_blocks:
                    continue
                new_name = block_map.get(cur)
                if not new_name or new_name not in known_blocks:
                    continue
                e.dxf.name = new_name
            except Exception:
                continue

    # Walk modelspace.
    _rewrite_insert_names_in(mech_doc.modelspace())
    # Walk every imported _STR_* block (recursively, in case of nested
    # INSERT → INSERT chains).
    def _rewrite_recursively(block):
        for be in list(block):
            try:
                if be.dxftype() != "INSERT":
                    continue
                cur = be.dxf.name
                if cur in known_blocks:
                    # Already in known — but recurse into the target
                    # block in case its contents have nested INSERTs.
                    nested = name_to_block.get(cur)
                    if nested is not None and nested is not block:
                        _rewrite_recursively(nested)
                    continue
                new_name = block_map.get(cur)
                if not new_name or new_name not in known_blocks:
                    continue
                be.dxf.name = new_name
                nested = name_to_block.get(new_name)
                if nested is not None and nested is not block:
                    _rewrite_recursively(nested)
            except Exception:
                continue

    # Start recursion from every _STR_* block (since their blocks
    # contain nested INSERTs).
    for blk_name in block_map.values():
        blk = name_to_block.get(blk_name)
        if blk is not None:
            _rewrite_recursively(blk)

    # imported_handles must include EVERY entity we added (clones +
    # Importer-imported non-INSERT entities) so Step 3 transforms them.
    # The clones have new handles assigned by add_entity(), so they're
    # not in `mech_handles`; the Importer-imported non-INSERT entities
    # retain their original STR handles (also not in `mech_handles`).
    # Both are present in mech_doc.modelspace now.
    imported_handles = {
        e.dxf.handle for e in mech_doc.modelspace()
        if e.dxf.hasattr("handle") and e.dxf.handle not in mech_handles
    }

    # Step 3: pre-scale (bake INSERT pos+xscale) once per block, then
    # transform STR entities.
    #
    # CRITICAL: each `_STR_*` block can be referenced by multiple
    # INSERTs in modelspace. If we pre-scale the block on every
    # INSERT iteration, the second INSERT's pre-scaling accumulates on
    # top of the first one's already-baked coords — yielding wildly
    # offset block content (test-a-2 case, where 11 INSERTs referenced
    # the same `A$C*` block, and pre-scaling ran 11× → block content
    # at `(2.4M, -1.7M)` instead of normal world coords).
    #
    # Fix: track which blocks have already been pre-baked, and only
    # reset the INSERT position to `(0,0,0)` on subsequent iterations.
    prescaled_blocks: set[int] = set()
    for e in list(mech_doc.modelspace()):
        try:
            if e.dxf.hasattr("handle") and e.dxf.handle in imported_handles:
                if e.dxftype() == "INSERT":
                    src_block = e.dxf.name
                    name_to_block = _block_lookup(mech_doc)
                    block = name_to_block.get(src_block)
                    if block is not None:
                        block_id = id(block)
                        if block_id not in prescaled_blocks:
                            prescaled_blocks.add(block_id)
                            sx = e.dxf.xscale or 1.0
                            sy = e.dxf.yscale if e.dxf.hasattr("yscale") and e.dxf.yscale else sx
                            ip_x, ip_y = e.dxf.insert[0], e.dxf.insert[1]
                            for be in list(block):
                                _prescale_entity(be, sx, sy, ip_x, ip_y)
                    # Always reset the INSERT position to origin so
                    # the (already-pre-baked) block content lives at
                    # world coords while the INSERT itself sits at (0,0).
                    e.dxf.insert = (0.0, 0.0, 0.0)
                    e.dxf.xscale = 1.0
                    e.dxf.yscale = 1.0
        except Exception:
            pass

    for e in list(mech_doc.modelspace()):
        try:
            if e.dxf.hasattr("handle") and e.dxf.handle in imported_handles:
                _transform_entity_inplace(e, ax, ay, bx, by)
        except Exception:
            pass

    # Build name → block lookup by iteration (NOT via `mech_doc.blocks`
    # `in` check, which is stale for renamed blocks — `block_record.rename`
    # updates the block_record but doesn't refresh the `Blocks.__contains__`
    # index).
    name_to_block = _block_lookup(mech_doc)

    for block_name in block_map.values():
        block = name_to_block.get(block_name)
        if block is None:
            continue
        for e in list(block):
            try:
                _transform_entity_inplace(e, ax, ay, bx, by)
            except Exception:
                pass

    # Step 4: rename STR layers to preserve color/avoid collisions.
    # Look up source-layer color and use it on the new _STR_* layer.
    str_layers_in_use = set()
    for e in list(mech_doc.modelspace()):
        try:
            if e.dxf.hasattr("handle") and e.dxf.handle in imported_handles:
                layer = e.dxf.layer
                if not layer.startswith(STR_PREFIX):
                    str_layers_in_use.add(layer)
        except Exception:
            pass
    name_to_block = _block_lookup(mech_doc)
    for block_name in block_map.values():
        block = name_to_block.get(block_name)
        if block is None:
            continue
        for e in list(block):
            try:
                layer = e.dxf.layer
                if not layer.startswith(STR_PREFIX):
                    str_layers_in_use.add(layer)
            except Exception:
                pass

    existing_layers = {l.dxf.name for l in mech_doc.layers}
    for layer in str_layers_in_use:
        new_name = STR_PREFIX + layer
        if new_name not in existing_layers:
            try:
                src_layer = str_doc_obj.layers.get(name=layer)
                layer_color = src_layer.dxf.color if (src_layer is not None
                                                      and src_layer.dxf.hasattr("color")) else 7
            except Exception:
                layer_color = 7
            mech_doc.layers.add(name=new_name, color=layer_color)
            existing_layers.add(new_name)

    layer_renames = {old: STR_PREFIX + old for old in str_layers_in_use}
    for e in list(mech_doc.modelspace()):
        try:
            if e.dxf.hasattr("handle") and e.dxf.handle in imported_handles:
                if e.dxf.layer in layer_renames:
                    e.dxf.layer = layer_renames[e.dxf.layer]
        except Exception:
            pass
    name_to_block = _block_lookup(mech_doc)
    for block_name in block_map.values():
        block = name_to_block.get(block_name)
        if block is None:
            continue
        for e in list(block):
            try:
                if e.dxf.layer in layer_renames:
                    e.dxf.layer = layer_renames[e.dxf.layer]
            except Exception:
                pass

    # MECH layers: untouched (preserve original colors per A's policy)


# ─────────────────────────────────────────────────────────────────────────────
# Strategy B — 2-block wrap, force red/blue
# ─────────────────────────────────────────────────────────────────────────────

def _strategy_B(mech_doc, str_doc_obj, scale_x, scale_y, tx, ty):
    """Wrap MECH in MECH_DRAWING block, STR in STR_DRAWING block.
    STR block INSERTed at (tx, ty) with xscale=scale_x.
    Force MECH layers→red, STR layers→blue.

    Uses Importer (same as strategy A) to copy STR content into
    mech_doc — this preserves block-table dependencies so no entities
    are dropped. Then we move just the imported STR entities from
    modelspace into STR_DRAWING, and the original MECH entities into
    MECH_DRAWING. Modelspace ends up with exactly 2 INSERTs."""
    BLUE_PREFIX = "_BLUE_"
    MECH_BLOCK = "MECH_DRAWING"
    STR_BLOCK = "STR_DRAWING"

    # 0) Snapshot the original MECH modelspace entity handles so we can
    #    identify them later (the rest after Importer finishes are STR).
    mech_handles = {e.dxf.handle for e in mech_doc.modelspace()
                    if e.dxf.hasattr("handle")}

    # 1) Import STR blocks (with _BLUE_ prefix to avoid name collisions).
    imp = Importer(str_doc_obj, mech_doc)
    for str_block in list(str_doc_obj.blocks):
        if str_block.name.startswith("*"):
            continue
        target = BLUE_PREFIX + str_block.name
        if target in mech_doc.blocks:
            try:
                mech_doc.blocks.delete_block(target, safe=False)
            except Exception:
                pass
        try:
            new_name = imp.import_block(str_block.name)
            # Force rename to _BLUE_ prefix even if no collision (Importer
            # doesn't add a prefix by default — it only renames on collision).
            if new_name != target:
                try:
                    blk = mech_doc.blocks.get(name=new_name)
                    if blk is not None:
                        blk.block_record.rename(target)
                except Exception:
                    pass
        except Exception:
            pass
    try:
        imp.finalize()
    except Exception:
        pass

    # 2) Import STR modelspace entities into mech_doc.modelspace().
    imp2 = Importer(str_doc_obj, mech_doc)
    for ent in list(str_doc_obj.modelspace()):
        try:
            imp2.import_entity(ent, target_layout=mech_doc.modelspace())
        except Exception:
            pass
    try:
        imp2.finalize()
    except Exception:
        pass

    # 3) Identify the just-imported STR entities (handles NOT in mech_handles).
    imported_str_handles = {
        e.dxf.handle for e in mech_doc.modelspace()
        if e.dxf.hasattr("handle") and e.dxf.handle not in mech_handles
    }

    # 4) Build STR_DRAWING block: move imported STR entities there.
    #    CRITICAL: unlink from current layout BEFORE add_entity — otherwise
    #    ezdxf raises DXFStructureError because the entity is still owned
    #    by modelspace.
    str_drawing = mech_doc.blocks.new(name=STR_BLOCK)
    for ent in list(mech_doc.modelspace()):
        try:
            if ent.dxf.hasattr("handle") and ent.dxf.handle in imported_str_handles:
                mech_doc.modelspace().unlink_entity(ent)
                str_drawing.add_entity(ent)
        except Exception:
            pass

    # 5) Build MECH_DRAWING block: move MECH's original entities there.
    #    Also clear explicit color overrides on MECH entities so they pick
    #    up the red layer color (without this, source entities with
    #    color=7 stay white even on a red layer).
    mech_drawing = mech_doc.blocks.new(name=MECH_BLOCK)
    for ent in list(mech_doc.modelspace()):
        try:
            mech_doc.modelspace().unlink_entity(ent)
            mech_drawing.add_entity(ent)
        except Exception:
            pass
    # Clear explicit colors recursively on MECH entities (everything not in
    # _BLUE_* sub-blocks).
    def _clear_mech_colors(block):
        for ent in list(block):
            try:
                if ent.dxf.hasattr("color"):
                    ent.dxf.color = 256
            except Exception:
                pass
            if ent.dxftype() == "INSERT":
                sub = mech_doc.blocks.get(name=ent.dxf.name)
                if sub is not None and not ent.dxf.name.startswith(BLUE_PREFIX):
                    _clear_mech_colors(sub)
    _clear_mech_colors(mech_drawing)

    # 6) Layer renames for STR (prefix all layers used by imported STR).
    #    Must walk into every _BLUE_* sub-block too — strategy A's layer
    #    logic only looked at str_drawing and missed layers referenced
    #    inside GridLine-*, Unknown-*, etc. The entities inside those
    #    blocks reference source layers (1, 2, 4, 10) which exist in
    #    mech_doc with their original colors — so without rewriting
    #    them, the STR content renders in MECH's colors.
    str_layers_in_use = set()
    # Walk the wrapper block
    for ent in list(str_drawing):
        try:
            str_layers_in_use.add(ent.dxf.layer)
        except Exception:
            pass
    # Walk every imported _BLUE_* sub-block (recursively, in case of nested)
    def _collect_layers(block):
        for ent in list(block):
            try:
                str_layers_in_use.add(ent.dxf.layer)
            except Exception:
                pass
            if ent.dxftype() == "INSERT":
                sub = mech_doc.blocks.get(name=ent.dxf.name)
                if sub is not None:
                    _collect_layers(sub)

    blue_blocks = [b for b in mech_doc.blocks if b.name.startswith(BLUE_PREFIX)]
    for blk in blue_blocks:
        _collect_layers(blk)

    existing_layers = {l.dxf.name for l in mech_doc.layers}
    for layer in str_layers_in_use:
        if layer.startswith(BLUE_PREFIX):
            continue
        new_name = BLUE_PREFIX + layer
        if new_name not in existing_layers:
            try:
                src_layer = str_doc_obj.layers.get(name=layer)
                layer_color = src_layer.dxf.color if (src_layer is not None
                                                      and src_layer.dxf.hasattr("color")) else 7
            except Exception:
                layer_color = 7
            mech_doc.layers.add(name=new_name, color=layer_color)
            existing_layers.add(new_name)

    renames = {old: BLUE_PREFIX + old for old in str_layers_in_use
               if not old.startswith(BLUE_PREFIX)}
    # Apply renames to entities in wrapper AND every _BLUE_* sub-block.
    # Also clear explicit color overrides (color=7, etc.) so entities pick
    # up the layer's color (which is now blue). Without this, source
    # entities with color=7 stay white even though they're on a blue layer.
    def _apply_renames(block):
        for ent in list(block):
            try:
                if ent.dxf.hasattr("layer") and ent.dxf.layer in renames:
                    ent.dxf.layer = renames[ent.dxf.layer]
                # Reset explicit color to BYLAYER (256) so the entity
                # inherits the layer color.
                if ent.dxf.hasattr("color"):
                    ent.dxf.color = 256
            except Exception:
                pass
            if ent.dxftype() == "INSERT":
                sub = mech_doc.blocks.get(name=ent.dxf.name)
                if sub is not None:
                    _apply_renames(sub)

    _apply_renames(str_drawing)
    for blk in blue_blocks:
        _apply_renames(blk)

    # 7) Force colors: every non-_BLUE_ layer → red; every _BLUE_ layer → blue.
    for L in mech_doc.layers:
        try:
            if L.dxf.name.startswith(BLUE_PREFIX):
                L.dxf.color = STR_COLOR
            else:
                L.dxf.color = MECH_COLOR
        except Exception:
            pass

    # 8) Place the two INSERTs.
    mech_doc.modelspace().add_blockref(MECH_BLOCK, (0, 0))
    mech_doc.modelspace().add_blockref(
        STR_BLOCK, (tx, ty),
        dxfattribs={"xscale": scale_x, "yscale": scale_y},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Strategy C — 1-block wrap, force red/blue
# ─────────────────────────────────────────────────────────────────────────────

def _strategy_C(mech_doc, str_doc_obj, ax, ay, bx, by):
    """Wrap everything in OVERLAY_DRAWING block, 1 INSERT at origin.
    Forces red/blue like B."""
    OVERLAY_BLOCK = "OVERLAY_DRAWING"

    # Run strategy A first — this imports STR into mech_doc.modelspace()
    # with the affine baked into each entity.
    _strategy_A(mech_doc, str_doc_obj, ax, ay, bx, by)

    # Now move every entity from modelspace into OVERLAY_DRAWING.
    # Use unlink+add_entity pattern (same fix as strategy B) so INSERTs
    # transfer cleanly without DXFStructureError.
    overlay_block = mech_doc.blocks.new(name=OVERLAY_BLOCK)
    for ent in list(mech_doc.modelspace()):
        try:
            mech_doc.modelspace().unlink_entity(ent)
            overlay_block.add_entity(ent)
        except Exception:
            pass

    # Force colors: red for MECH, blue for STR (prefixed _STR_ by A).
    for L in mech_doc.layers:
        try:
            if L.dxf.name.startswith("_STR_"):
                L.dxf.color = STR_COLOR
            else:
                L.dxf.color = MECH_COLOR
        except Exception:
            pass

    # Clear explicit color overrides so entities pick up the layer color.
    # Source entities often have color=7 (white) or other values set as
    # explicit overrides, which would override our layer color force.
    def _clear_explicit_colors(block):
        for ent in list(block):
            try:
                if ent.dxf.hasattr("color"):
                    ent.dxf.color = 256
            except Exception:
                pass
            if ent.dxftype() == "INSERT":
                sub = mech_doc.blocks.get(name=ent.dxf.name)
                if sub is not None:
                    _clear_explicit_colors(sub)
    _clear_explicit_colors(overlay_block)

    # Place the single INSERT at origin.
    mech_doc.modelspace().add_blockref(OVERLAY_BLOCK, (0, 0))


# ─────────────────────────────────────────────────────────────────────────────
# Stage entrypoint
# ─────────────────────────────────────────────────────────────────────────────

def run(job_id: str, *, pair_dir: Path | None = None,
        strategy: str | None = None) -> None:
    """Apply the fitted transform with the chosen strategy.

    `strategy` is per-pair when supplied (Slice 2 multi-pair flow);
    otherwise we fall back to `JobState.strategy` so the legacy single-
    pair pipeline behaves bit-equivalently.
    """
    d = job_dir(job_id)
    if pair_dir is not None:
        root = Path(pair_dir).parent
        cleaned = sorted(Path(pair_dir).glob("*_clean.dxf"))
    else:
        root = d
        cleaned = sorted((d / "cleaned").glob("*_clean.dxf"))
    if len(cleaned) != 2:
        raise RuntimeError(f"Need 2 cleaned DXFs; found {len(cleaned)}")
    mech_path = cleaned[0]
    str_path = cleaned[1]

    # Read transform
    transform_path = root / "transform.json"
    if not transform_path.exists():
        raise RuntimeError("transform.json missing — run fit first")
    t = json.loads(transform_path.read_text())
    ax, ay, bx, by = t["scale_x"], t["scale_y"], t["tx"], t["ty"]

    # Resolve strategy: per-pair arg wins (Slice 2); legacy single-pair
    # flows leave it None and we read from JobState.
    if strategy is None:
        job = load_job(job_id)
        if job is None:
            raise RuntimeError(f"Job {job_id} disappeared")
        strategy = job.strategy

    mech_doc = ezdxf.readfile(str(mech_path))
    str_doc = ezdxf.readfile(str(str_path))

    if strategy == "A":
        _strategy_A(mech_doc, str_doc, ax, ay, bx, by)
    elif strategy == "B":
        _strategy_B(mech_doc, str_doc, scale_x=ax, scale_y=ay, tx=bx, ty=by)
    elif strategy == "C":
        _strategy_C(mech_doc, str_doc, ax, ay, bx, by)
    else:
        raise RuntimeError(f"Unknown strategy: {strategy!r}")

    # Save output
    out_dir = root / "out"
    out_dir.mkdir(exist_ok=True)
    out_dxf = out_dir / "superimposed.dxf"
    mech_doc.saveas(str(out_dxf))
