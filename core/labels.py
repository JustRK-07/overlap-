#!/usr/bin/env python3
"""core/labels.py — Stage 3.5a.

Scans both uploaded drawings' labels (modelspace + every non-anonymous
block def), computes the cross-drawing agreement set, and writes the
handle/block lists that the cleaner (stage 4) uses to decide what NOT
to delete.

This stage runs **after** convert (stage 3) and **before** clean_policy
(stage 3.5b) and clean (stage 4). It reads the raw post-convert DXFs
from `jobs/<id>/cleaned/` and writes:

  jobs/<id>/stages/labels.json                  — full per-drawing label catalog
  jobs/<id>/stages/cross_drawing_agreement.json — intersection + per-prefix buckets
  jobs/<id>/stages/protected_handles.json       — {dxf_name: [handle, ...]}
  jobs/<id>/stages/protected_blocks.json        — {dxf_name: [block_name, ...]}

The cleaner reads the two `_handles.json` / `_blocks.json` files and
threads them through every strip helper so cross-drawing-agreement
labels survive regardless of which container they live in.

When the two drawings don't share any labels (no agreement), we write
empty protected-* files — the cleaner falls back to today's
regex/prefix-list behaviour.

Why label-first: the previous pipeline cleaned first and tried to
detect labels after. That worked when labels lived directly in
modelspace, but for STR drawings where labels live inside `GridLine-*`
named block defs, the cleaner could delete every `GridLine-*` INSERT
(and with it, the labels) before stage 5 ever ran. Inverting the order
makes the data drive the cleaner's decisions instead of heuristics.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from jobs import job_dir

from .axes import LabelCandidate, extract_labels


def scan_labels_for_job(job_id: str) -> dict:
    """Stage 3.5a entrypoint. Run after stage 3 (convert).

    Walks every DXF in `jobs/<id>/cleaned/`, extracts labels, computes
    the cross-drawing agreement set, writes the four JSON outputs.

    Returns a dict with the computed agreement for the caller's logs.
    Empty dict when there are no DXFs or no agreement.
    """
    d = job_dir(job_id)
    cleaned_dir = d / "cleaned"
    stages_dir = d / "stages"
    stages_dir.mkdir(exist_ok=True)

    # Find DXFs that the convert stage produced (skip any pre-existing
    # `_clean.dxf` outputs).
    dxfs = sorted(
        p for p in cleaned_dir.glob("*.dxf")
        if not p.stem.endswith("_clean")
    )

    if not dxfs:
        # Nothing to scan — write empty outputs so downstream stages
        # gracefully fall back to their no-protection defaults.
        _write_empty(stages_dir)
        return {}

    import ezdxf  # imported here to avoid heavy import at module load

    per_drawing: dict[str, list[dict]] = {}
    for dxf in dxfs:
        doc = ezdxf.readfile(str(dxf))
        labels = extract_labels(doc)
        per_drawing[dxf.name] = [_label_to_dict(c) for c in labels]

    labels_path = stages_dir / "labels.json"
    labels_path.write_text(json.dumps(per_drawing, indent=2))

    # Cross-drawing agreement: case-insensitive raw-text intersection
    # across the per-drawing text sets.
    text_sets: dict[str, set[str]] = {
        name: {lab["text"].upper() for lab in labs}
        for name, labs in per_drawing.items()
    }

    if len(text_sets) < 2:
        agreement_texts: set[str] = set()
    else:
        # Intersection across all drawings (not just pairwise).
        iter_sets = iter(text_sets.values())
        agreement_texts = next(iter_sets).copy()
        for s in iter_sets:
            agreement_texts &= s

    # Bucket the agreement labels by their prefix (for stage 3.5b's
    # regex derivation, which uses the same clustering).
    agreement_by_prefix: dict[str, list[dict]] = defaultdict(list)
    agreement_blocks_by_dxf: dict[str, set[str]] = defaultdict(set)
    agreement_handles_by_dxf: dict[str, set[str]] = defaultdict(set)
    # INSERTs that reference blocks holding an agreement label — also
    # protected so the cleaner doesn't strip them. Without this, fit.py
    # walks modelspace looking for `GridLine-*` INSERTs and finds none.
    agreement_insert_handles_by_dxf: dict[str, set[str]] = defaultdict(set)

    # Build {dxf_name -> {block_name: [INSERT handle, ...]}} so we can
    # resolve each protected block to its INSERTs in modelspace.
    block_to_insert_handles: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for dxf_path_str, doc in ((str(p), __import__("ezdxf").readfile(str(p))) for p in dxfs):
        dxf_name = Path(dxf_path_str).name
        for ins in doc.modelspace():
            try:
                if ins.dxftype() != "INSERT":
                    continue
                bn = ins.dxf.name
                h = ins.dxf.handle
                if bn and h:
                    block_to_insert_handles[dxf_name][bn].add(h)
            except Exception:
                continue

    for dxf_name, labs in per_drawing.items():
        for lab in labs:
            if lab["text"].upper() not in agreement_texts:
                continue
            agreement_by_prefix[lab["prefix"]].append({**lab, "drawing": dxf_name})
            if lab.get("block_name"):
                agreement_blocks_by_dxf[dxf_name].add(lab["block_name"])
                # Also collect every INSERT that references this block,
                # so the cleaner doesn't strip them.
                for h in block_to_insert_handles.get(dxf_name, {}).get(
                        lab["block_name"], []):
                    agreement_insert_handles_by_dxf[dxf_name].add(h)
            if lab.get("handle"):
                agreement_handles_by_dxf[dxf_name].add(lab["handle"])

    cross_path = stages_dir / "cross_drawing_agreement.json"
    cross_path.write_text(json.dumps({
        "agreement_texts":      sorted(agreement_texts),
        "agreement_by_prefix":  {p: sorted(labs, key=lambda x: x["text"])
                                 for p, labs in agreement_by_prefix.items()},
        "drawings_scanned":     sorted(per_drawing.keys()),
    }, indent=2))

    # Write the protected-handles + protected-blocks files. The cleaner
    # keys these by DXF filename. If agreement is empty, both are `{}`.
    # Union the TEXT-label handles with the INSERT handles that
    # reference protected blocks — the cleaner would otherwise strip
    # those INSERTs and break `fit.py`, which looks up axes by walking
    # `GridLine-*` INSERTs in modelspace.
    merged_handles_by_dxf: dict[str, set[str]] = defaultdict(set)
    for name, hs in agreement_handles_by_dxf.items():
        merged_handles_by_dxf[name].update(hs)
    for name, hs in agreement_insert_handles_by_dxf.items():
        merged_handles_by_dxf[name].update(hs)
    handles_path = stages_dir / "protected_handles.json"
    handles_path.write_text(json.dumps(
        {name: sorted(handles) for name, handles in merged_handles_by_dxf.items()},
        indent=2,
    ))
    blocks_path = stages_dir / "protected_blocks.json"
    blocks_path.write_text(json.dumps(
        {name: sorted(blocks) for name, blocks in agreement_blocks_by_dxf.items()},
        indent=2,
    ))

    return {
        "drawings_scanned":     sorted(per_drawing.keys()),
        "agreement_count":      len(agreement_texts),
        "agreement_by_prefix":  {p: len(labs) for p, labs in agreement_by_prefix.items()},
    }


def _label_to_dict(c: LabelCandidate) -> dict:
    """Serialise a LabelCandidate to a JSON-safe dict."""
    try:
        handle = c.entity.dxf.handle if c.entity is not None else None
    except Exception:
        handle = None
    return {
        "text":       c.text,
        "prefix":     c.prefix,
        "suffix":     c.suffix,
        "container":  c.container,
        "block_name": c.block_name,
        "position":   list(c.position) if c.position else None,
        "handle":     handle,
    }


def _write_empty(stages_dir: Path) -> None:
    """Write empty protected-* files so the cleaner finds them and falls back."""
    (stages_dir / "labels.json").write_text("{}")
    (stages_dir / "cross_drawing_agreement.json").write_text(json.dumps({
        "agreement_texts": [], "agreement_by_prefix": {}, "drawings_scanned": [],
    }, indent=2))
    (stages_dir / "protected_handles.json").write_text("{}")
    (stages_dir / "protected_blocks.json").write_text("{}")


def run(job_id: str) -> None:
    """Pipeline-friendly entrypoint — wraps scan_labels_for_job + logs summary."""
    summary = scan_labels_for_job(job_id)
    if not summary:
        print("  labels: no DXFs to scan")
        return
    n = summary.get("agreement_count", 0)
    drawings = summary.get("drawings_scanned", [])
    print(f"  labels: scanned {len(drawings)} drawing(s), {n} cross-drawing agreement label(s)")
    by_prefix = summary.get("agreement_by_prefix", {})
    if by_prefix:
        for prefix, count in by_prefix.items():
            print(f"    prefix {prefix!r}: {count} agreement label(s)")
