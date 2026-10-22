"""core/clean_policy.py — CleaningPolicy dataclass + auto-derivation.

A `CleaningPolicy` carries every project-specific knob the cleaner uses:
  - The grid-label regex (which TEXT/MTEXT entities are kept)
  - The STR block prefixes (which INSERTs we preserve)
  - The MECH block prefixes (which INSERTs we preserve)
  - The marker layer (which layer carries callout-balloon hexagons)
  - The annotation-only layers (which layers we always strip)
  - The known-orphan block names (always safe to delete)

`DEFAULT_POLICY` mirrors the historical hardcoded values so the TP-104
fixture produces identical output.

`derive_policy(doc)` scans a cleaned-up DXF (post-convert, pre-clean)
and returns a fully-populated `CleaningPolicy` based on what's actually
in the file.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field


@dataclass
class CleaningPolicy:
    """All project-specific knobs the cleaner needs. Fully serialisable."""

    # TEXT/MTEXT entities matching this regex are KEPT (not stripped).
    # Built from a convention suggestion — e.g. r"^\s*TP104-[A-D1-3]\s*$"
    # The default mirrors the historical TP-104 hardcoded value so the
    # TP-104 fixture pipeline produces identical output if no policy has
    # been derived yet.
    label_regex: str = r"^\s*TP104-[A-D1-3]\s*$"

    # INSERT entity block-name prefixes that survive STR cleaning.
    # Other INSERTs are stripped (along with their contents).
    keep_block_prefixes_str: list[str] = field(
        default_factory=lambda: [
            "Part-", "Bolt-", "Connection-", "GridLine-", "Line-",
        ]
    )

    # INSERT block-name prefixes that survive MECH cleaning.
    # Anonymous A$C* blocks hold real equipment geometry (HATCH,
    # LWPOLYLINE, CIRCLE) so we keep them by default.
    keep_block_prefixes_mech: list[str] = field(
        default_factory=lambda: ["A$C", "RTR"]
    )

    # The layer that carries callout-balloon hexagons (MECH only).
    # 6-vertex closed LWPOLYLINE entities on this layer get stripped.
    marker_layer: str = "BOX"

    # Layers that are always documentation, never real geometry.
    # All entities on these layers are stripped.
    annotation_layers: list[str] = field(
        default_factory=lambda: [
            "DIM",        # dimensioning
            "CIVIL",      # civil markers
            "AM_",        # annotation markers (AM_5, AM_VIEWS, etc.)
            "REV-",       # revision triangles (REV-A, REV-B, REV-C)
            "DEFPOINTS",  # dimension definition points layer
            "AM_VIEWS",
            "TITLE",      # title block
            "BORDER",     # drawing border
            "FRAME",      # view frame
        ]
    )

    # Block names always safe to delete (in addition to unreferenced ones).
    known_orphan_blocks: list[str] = field(
        default_factory=lambda: ["A$C2135de20"]
    )

    def to_dict(self) -> dict:
        return {
            "label_regex": self.label_regex,
            "keep_block_prefixes_str": self.keep_block_prefixes_str,
            "keep_block_prefixes_mech": self.keep_block_prefixes_mech,
            "marker_layer": self.marker_layer,
            "annotation_layers": self.annotation_layers,
            "known_orphan_blocks": self.known_orphan_blocks,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CleaningPolicy":
        """Build a CleaningPolicy from a JSON dict. Missing fields fall
        back to the dataclass default. Used when loading from job state.
        """
        import inspect
        valid_fields = {f for f in inspect.signature(cls).parameters}
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(**filtered)


# Historical defaults — used when no policy has been derived yet
# (e.g. for legacy job files, or for tests).
DEFAULT_POLICY = CleaningPolicy()


# ─────────────────────────────────────────────────────────────────────────────
# Auto-derivation
# ─────────────────────────────────────────────────────────────────────────────

def _build_label_regex(prefix: str, suffixes: list[str]) -> str:
    """Build a regex matching exactly: prefix + (one of suffixes).

    The regex anchors with `^...$` and allows whitespace around the label
    to match the historical hardcoded behaviour for TP-104 labels.
    """
    if not suffixes:
        return DEFAULT_POLICY.label_regex
    sfx_alt = "|".join(re.escape(s) for s in suffixes)
    prefix_esc = re.escape(prefix) if prefix else ""
    return rf"^\s*{prefix_esc}({sfx_alt})\s*$"


def derive_block_prefixes(doc, *, top_n: int = 5) -> list[str]:
    """Auto-derive STR block-name prefixes from the drawing.

    Walks every INSERT in modelspace, extracts the part before the first
    `-` in its block name (Part-, Bolt-, etc.) and returns the top-N
    most-common prefixes. Falls back to DEFAULT if no INSERTs found.
    """
    prefixes: Counter = Counter()
    for ins in doc.modelspace():
        if ins.dxftype() != "INSERT":
            continue
        try:
            nm = ins.dxf.name
        except Exception:
            continue
        if "-" in nm:
            prefix = nm.split("-", 1)[0] + "-"
            prefixes[prefix] += 1
    if not prefixes:
        return DEFAULT_POLICY.keep_block_prefixes_str
    return [p for p, _ in prefixes.most_common(top_n)]


def _strip_metadata_labels(labels):
    """Drop LabelCandidates that look like metadata rather than grid axes.

    Filters out:
      - Empty labels
      - Beam-section / bolt-grade labels (ISMB 600, UB 533, W12x26, M20)
      - Labels whose suffix is a 4-digit number (looks like a year)
    """
    out = []
    for c in labels:
        if not c.text.strip():
            continue
        if _looks_like_beam_section(c.text):
            continue
        # Skip year-like pure-digit suffixes
        if c.suffix.isdigit() and len(c.suffix) == 4:
            continue
        out.append(c)
    return out


import ezdxf
from .axes import LabelCandidate, extract_labels, _build_world_gridline_cache, _compute_world_positions, _annotate_proximity, _adaptive_threshold, _looks_like_beam_section, _score_cluster


def derive_policy_from_drawing_pair(mech_path, str_path) -> "CleaningPolicy":
    """Derive a CleaningPolicy from two pre-cleaned DXFs (MECH + STR).

    Strategy (rather than calling axes.suggest() per-drawing):
      1. Extract all LabelCandidates from both drawings; annotate each
         with gridline-proximity info (on_gridline + distance).
      2. Find labels that exist in BOTH drawings with identical raw text
         (case-insensitive). Those are the cross-drawing agreement set.
      3. Cluster those by prefix; pick the prefix with the highest
         proximity-aware score (count × mixed-bonus × gridline-proximity
         factor) → build a regex like `^prefix(s1|s2|...)$`.
      4. Auto-derive block prefixes by walking INSERTs.
      5. Fall back to DEFAULT if no agreement found.
    """
    mech_doc = ezdxf.readfile(str(mech_path))
    str_doc = ezdxf.readfile(str(str_path))

    mech_labels = _strip_metadata_labels(extract_labels(mech_doc))
    str_labels = _strip_metadata_labels(extract_labels(str_doc))

    # Transform block-def labels/gridlines into world coords, then
    # annotate each candidate with gridline-proximity info. The cache
    # is keyed by doc identity, so calling suggest() elsewhere doesn't
    # re-walk.
    for labels, doc in ((mech_labels, mech_doc), (str_labels, str_doc)):
        _compute_world_positions(doc, labels)
        world_gridlines = _build_world_gridline_cache(doc)
        threshold = _adaptive_threshold(doc)
        _annotate_proximity(labels, world_gridlines, threshold=threshold)

    # 1. Find labels shared (case-insensitive raw text) between drawings
    mech_texts = {c.text.upper() for c in mech_labels}
    str_texts = {c.text.upper() for c in str_labels}
    shared_texts = mech_texts & str_texts

    # 2. Filter to shared labels with prefix + suffix
    shared_labels = [c for c in mech_labels if c.text.upper() in shared_texts]
    if shared_labels:
        # Cluster by prefix; pick the highest-scoring prefix using the
        # proximity-aware scorer from axes.py.
        by_prefix: dict = defaultdict(list)
        for c in shared_labels:
            by_prefix[c.prefix].append(c)
        best_prefix = None
        best_score = -1.0
        best_axes: list[str] = []
        for prefix, items in by_prefix.items():
            suffixes = sorted({c.suffix for c in items})
            if len(suffixes) < 2:
                continue
            score = _score_cluster(items)
            if score > best_score:
                best_prefix = prefix
                best_score = score
                best_axes = suffixes
        if best_prefix is not None and best_axes:
            label_regex = _build_label_regex(best_prefix, best_axes)
        else:
            label_regex = DEFAULT_POLICY.label_regex
    else:
        # 3. No agreement — fall back to single-drawing clustering with
        # the same proximity-aware scorer (use union of both drawings).
        single_labels = mech_labels + str_labels
        if single_labels:
            by_prefix = defaultdict(list)
            for c in single_labels:
                by_prefix[c.prefix].append(c)
            best_prefix = None
            best_score = -1.0
            best_axes = []
            for prefix, items in by_prefix.items():
                suffixes = sorted({c.suffix for c in items})
                if len(suffixes) < 2:
                    continue
                score = _score_cluster(items)
                if score > best_score:
                    best_prefix = prefix
                    best_score = score
                    best_axes = suffixes
            if best_prefix is not None:
                label_regex = _build_label_regex(best_prefix, best_axes)
            else:
                label_regex = DEFAULT_POLICY.label_regex
        else:
            label_regex = DEFAULT_POLICY.label_regex

    # 4. Auto-derive block prefixes — average of MECH + STR
    mech_prefixes = derive_block_prefixes(mech_doc)
    str_prefixes = derive_block_prefixes(str_doc)
    # Pick the longer of the two sets (assumes STR has more named blocks)
    keep_block_prefixes_str = str_prefixes if len(str_prefixes) >= len(mech_prefixes) else mech_prefixes

    return CleaningPolicy(
        label_regex=label_regex,
        keep_block_prefixes_str=keep_block_prefixes_str,
        # MECH prefixes stay as default (A$C, RTR) — they're universal
        # across Smart3D-style anonymous block geometries.
        keep_block_prefixes_mech=DEFAULT_POLICY.keep_block_prefixes_mech,
        marker_layer=DEFAULT_POLICY.marker_layer,
        annotation_layers=DEFAULT_POLICY.annotation_layers,
        known_orphan_blocks=DEFAULT_POLICY.known_orphan_blocks,
    )


def run(job_id: str) -> None:
    """Stage 3.5b entrypoint — runs after labels scan (3.5a) and before clean (4).

    Reads the DXFs in jobs/<id>/cleaned/ (post-stage-3, pre-stage-4 — TEXT
    labels are still intact) and derives a CleaningPolicy from the agreement
    between the two drawings. Writes the policy to:
      - jobs/<id>/stages/clean_policy.json
      - jobs/<id>/job.json  (so the UI can show it in the convention picker)

    When stage 3.5a (`core.labels`) has produced `cross_drawing_agreement.json`
    with at least one prefix bucket, we also augment `keep_block_prefixes_str`
    with the unique block-name prefixes that contain an agreement label. This
    is a belt-and-braces measure on top of the protected-handle list — even
    if the cleaner somehow misses the handle check, the INSERT filter will
    keep the block that holds the agreement label.
    """
    from jobs import job_dir, load_job, save_job
    d = job_dir(job_id)
    cleaned_dir = d / "cleaned"
    stages_dir = d / "stages"
    stages_dir.mkdir(exist_ok=True)

    cleaned = sorted(cleaned_dir.glob("*.dxf"))
    if len(cleaned) < 2:
        # Fallback: one input DXF (rare). Derive from whatever we have.
        if cleaned:
            from .axes import extract_labels_for_path
            labels = extract_labels_for_path(cleaned[0])
            if labels:
                suffixes = sorted({c.suffix for c in labels})
                prefix = labels[0].prefix or ""
                label_regex = _build_label_regex(prefix, suffixes)
                policy = CleaningPolicy(label_regex=label_regex)
            else:
                policy = CleaningPolicy()
        else:
            policy = CleaningPolicy()
    else:
        policy = derive_policy_from_drawing_pair(cleaned[0], cleaned[1])

    # Augment with agreement-derived block prefixes (label-first safety net).
    agreement_path = stages_dir / "cross_drawing_agreement.json"
    if agreement_path.exists():
        try:
            cross = json.loads(agreement_path.read_text())
            agreement_by_prefix = cross.get("agreement_by_prefix") or {}
            # Collect every block-name prefix that holds at least one
            # agreement label — `GridLine-`, `Line-`, etc. These get added
            # to keep_block_prefixes_str so the INSERT filter preserves them.
            extra_prefixes = set()
            for labs in agreement_by_prefix.values():
                for lab in labs:
                    bn = lab.get("block_name")
                    if not bn or "-" not in bn:
                        continue
                    extra_prefixes.add(bn.split("-", 1)[0] + "-")
            if extra_prefixes:
                merged = list(dict.fromkeys(
                    list(policy.keep_block_prefixes_str) + sorted(extra_prefixes)
                ))
                policy = CleaningPolicy(
                    **{**policy.to_dict(), "keep_block_prefixes_str": merged}
                )
        except Exception:
            pass

    # Write policy to disk for the cleaning stage to read
    (stages_dir / "clean_policy.json").write_text(
        json.dumps(policy.to_dict(), indent=2)
    )

    # Also surface on job.json stages dict so the UI can show it
    job = load_job(job_id)
    if job is not None:
        job.stages["clean_policy"] = policy.to_dict()
        save_job(job)

    print(f"  clean_policy: {_summarize_policy(policy)}")


def _summarize_policy(policy: CleaningPolicy) -> str:
    """One-line summary of the policy for the log."""
    # Strip our standard escape sequences to recover the visible parts
    rx = policy.label_regex
    visible = re.sub(r"\\([.\-\*\+\?\^\$])", lambda m: m.group(1), rx)
    visible = re.sub(r"\\\(|\\\)", "", visible)
    visible = re.sub(r"\^|\$", "", visible)
    visible = visible.replace("\\s*", "").strip()
    # Count alternatives in the (a|b|c) group
    parts = visible.split("(")
    if len(parts) > 1:
        in_group = parts[1].split(")")[0]
        axes = in_group.split("|")
        prefix_part = parts[0].replace("?", "").strip()
        return f'prefix="{prefix_part}" axes=[{", ".join(axes)}]'
    return f"regex={policy.label_regex!r}"
