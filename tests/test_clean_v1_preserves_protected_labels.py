#!/usr/bin/env python3
"""test_clean_v1_preserves_protected_labels.py — label-first cleaner tests.

Covers:
  - Grid labels inside `GridLine-*` named blocks survive when the
    cross-drawing agreement set marks them protected.
  - Beam-section / bolt-grade labels still get stripped even when
    the cleaner reads a handle-protection list.
  - A protected block def survives orphan-block cleanup even when
    no INSERT references it.
  - When no protected_handles.json exists, the cleaner behaves
    exactly as before (default policy, no exceptions).
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import ezdxf
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from jobs import JOBS_DIR, job_dir, save_job, create_job_legacy  # noqa: E402
import core.clean_v1 as clean_v1  # noqa: E402
import core.clean_common as clean_common  # noqa: E402
import core.clean_policy as clean_policy  # noqa: E402
from core.clean_policy import CleaningPolicy, DEFAULT_POLICY  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _make_str_like_dxf(path: Path) -> dict:
    """Build a STR-like DXF that mirrors the TP-104 STR failure mode:

    - 7 TP104-X TEXT entities live inside 7 GridLine-* named block defs.
    - The modelspace has INSERTs referencing those GridLine-* blocks.
    - Plus an ISMB 600 beam label (must still be stripped).

    Returns the per-label handle map for assertions.
    """
    doc = ezdxf.new()
    msp = doc.modelspace()

    handles = {}

    def _make_block(name: str, label: str, x: float, y: float):
        blk = doc.blocks.new(name=name)
        blk.add_line((0, 0), (x, y))
        h = blk.add_text(label, dxfattribs={"height": 5.0, "insert": (x, y)}).dxf.handle
        handles[label] = {"handle": h, "block": name}
        return blk

    suffixes = ["1", "2", "3", "A", "B", "C", "D"]
    prefix = "TP104-"
    for i, sfx in enumerate(suffixes):
        bn = f"GridLine-TP104-{sfx}"
        _make_block(bn, f"{prefix}{sfx}", x=100.0 * i, y=100.0 * i)
        msp.add_blockref(bn, insert=(100.0 * i, 100.0 * i))

    # Beam label (must be stripped, never protected).
    h_beam = msp.add_text(
        "ISMB 600",
        dxfattribs={"height": 3.0, "insert": (500.0, 500.0)},
    ).dxf.handle
    handles["ISMB 600"] = {"handle": h_beam, "block": None}

    doc.saveas(str(path))
    return handles


@pytest.fixture
def fx_isolated_jobs(monkeypatch, tmp_path):
    from jobs import JOBS_DIR
    monkeypatch.setattr("jobs.JOBS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def fx_str_dxf(fx_isolated_jobs):
    """Stage a STR-like DXF in <job>/cleaned/, return its path + handle map."""
    cleaned = fx_isolated_jobs / "abc" / "cleaned"
    cleaned.mkdir(parents=True, exist_ok=True)
    dxf_path = cleaned / "TP-104 STR GA.dxf"
    handles = _make_str_like_dxf(dxf_path)
    return dxf_path, handles


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_grid_labels_inside_named_block_survive_when_protected(fx_str_dxf):
    """Protected handles cause labels inside `GridLine-*` blocks to survive.

    Without the protection, the v1 cleaner would delete every
    `GridLine-*` INSERT and then orphan-block-clean the block defs,
    taking the labels with them. With the protection, both the
    INSERT and the label TEXT inside the block def survive.
    """
    dxf_path, handles = fx_str_dxf
    stages = dxf_path.parent.parent / "stages"
    stages.mkdir(exist_ok=True)
    out_dxf = dxf_path.parent / "TP-104 STR GA_clean.dxf"

    # Mark all 7 TP104-X handles protected.
    protected = {handles[f"TP104-{sfx}"]["handle"]
                 for sfx in ["1", "2", "3", "A", "B", "C", "D"]}
    protected_blocks = {handles[f"TP104-{sfx}"]["block"]
                        for sfx in ["1", "2", "3", "A", "B", "C", "D"]}
    (stages / "protected_handles.json").write_text(
        json.dumps({dxf_path.name: sorted(protected)})
    )
    (stages / "protected_blocks.json").write_text(
        json.dumps({dxf_path.name: sorted(protected_blocks)})
    )

    stats = clean_v1._clean_one(
        dxf_path, out_dxf, is_str=True, policy=DEFAULT_POLICY,
        protected=protected, protected_blocks=protected_blocks,
    )

    assert stats["error"] is None if "error" in stats else True  # no exception
    assert out_dxf.exists()

    # Re-open and check the labels are still there.
    doc2 = ezdxf.readfile(str(out_dxf))
    surviving = set()
    for e in doc2.modelspace():
        if e.dxftype() == "TEXT":
            surviving.add(e.dxf.text.strip())
    # Modelspace won't have them (they live inside blocks), but the
    # block defs must still contain them.
    for block in doc2.blocks:
        if block.name.startswith("*"):
            continue
        for e in block:
            if e.dxftype() == "TEXT":
                surviving.add(e.dxf.text.strip())

    for sfx in ["1", "2", "3", "A", "B", "C", "D"]:
        assert f"TP104-{sfx}" in surviving, (
            f"TP104-{sfx} was stripped despite being protected"
        )
    # Beam label must still be stripped.
    assert "ISMB 600" not in surviving, (
        "Beam-section label was protected — protected set was wrong"
    )

    # Stats should record what we protected.
    assert stats["protected_handles_total"] == 7
    assert stats["protected_entities_kept"] == 7


def test_beam_section_label_still_stripped(fx_str_dxf):
    """When only TP104-X handles are protected, ISMB 600 is still removed."""
    dxf_path, handles = fx_str_dxf
    out_dxf = dxf_path.parent / "TP-104 STR GA_clean.dxf"
    stages = dxf_path.parent.parent / "stages"
    stages.mkdir(exist_ok=True)

    # Only the grid labels are protected.
    protected = {handles[f"TP104-{sfx}"]["handle"]
                 for sfx in ["1", "2", "3", "A", "B", "C", "D"]}
    (stages / "protected_handles.json").write_text(
        json.dumps({dxf_path.name: sorted(protected)})
    )

    clean_v1._clean_one(
        dxf_path, out_dxf, is_str=True, policy=DEFAULT_POLICY,
        protected=protected,
    )

    doc2 = ezdxf.readfile(str(out_dxf))
    for e in doc2.modelspace():
        if e.dxftype() == "TEXT":
            assert e.dxf.text.strip() != "ISMB 600"


def test_protected_block_preserved_when_no_insert_references(fx_str_dxf):
    """If a GridLine-* block has no INSERT but holds a protected label,
    the orphan-block cleanup must NOT delete it."""
    dxf_path, handles = fx_str_dxf
    out_dxf = dxf_path.parent / "TP-104 STR GA_clean.dxf"

    # Protect the A-suffix block ONLY (no INSERTs reference it after
    # the cleaner deletes everything outside the protected set).
    protected = {handles["TP104-A"]["handle"]}
    protected_blocks = {handles["TP104-A"]["block"]}

    # Run the cleaner with a stripped-down keep list so the INSERT
    # filter actually deletes GridLine-* INSERTs.
    policy = CleaningPolicy(
        label_regex=r"^\s*TP104-[A-D1-3]\s*$",
        keep_block_prefixes_str=["Part-"],   # GridLine- NOT in keep list
    )

    clean_v1._clean_one(
        dxf_path, out_dxf, is_str=True, policy=policy,
        protected=protected, protected_blocks=protected_blocks,
    )

    doc2 = ezdxf.readfile(str(out_dxf))
    # The protected block must still exist and still contain its label.
    blk = doc2.blocks.get(handles["TP104-A"]["block"])
    assert blk is not None, "protected block was deleted by orphan cleanup"
    txts = [e.dxf.text.strip() for e in blk if e.dxftype() == "TEXT"]
    assert "TP104-A" in txts


def test_no_protected_file_means_default_behaviour(fx_str_dxf):
    """When protected_handles.json doesn't exist, the cleaner uses defaults
    (no protection). It still runs without errors and writes its output."""
    dxf_path, _ = fx_str_dxf
    out_dxf = dxf_path.parent / "TP-104 STR GA_clean.dxf"
    # No protected_handles.json on disk.
    stats = clean_v1._clean_one(
        dxf_path, out_dxf, is_str=True, policy=DEFAULT_POLICY,
    )
    assert out_dxf.exists()
    assert stats["protected_handles_total"] == 0
    assert stats["protected_entities_kept"] == 0


def test_is_protected_helper_handles_missing_handle():
    """Entities without a `.dxf.handle` attribute don't crash the helper."""
    class FakeEntity:
        dxf = type("DXF", (), {})()  # no .handle attr
    assert clean_common._is_protected(FakeEntity(), {"X"}) is False
    assert clean_common._is_protected(FakeEntity(), None) is False


def test_clean_policy_run_augments_keep_list_with_agreement_blocks(
        fx_isolated_jobs, tmp_path):
    """clean_policy.run adds GridLine- to keep_block_prefixes_str when
    agreement labels live inside GridLine-* blocks."""
    # Build a job with two cleaned DXFs that have shared TP104-X labels
    # (one in modelspace, one in a GridLine-* block).
    from jobs import create_job_legacy, save_job, job_dir
    job = create_job_legacy(
        drawing_a_name="a.dxf", drawing_a_size=10,
        drawing_b_name="b.dxf", drawing_b_size=10,
    )
    job_id = job.job_id
    d = job_dir(job_id)
    (d / "cleaned").mkdir(exist_ok=True)
    (d / "stages").mkdir(exist_ok=True)

    # Write minimal DXFs (the clean_policy stage doesn't need real
    # geometry — derive_policy_from_drawing_pair reads labels only).
    def _write_minimal(path: Path, label: str, in_block: bool):
        doc = ezdxf.new()
        if in_block:
            blk = doc.blocks.new(name="GridLine-X")
            blk.add_text(label, dxfattribs={"height": 5.0, "insert": (0, 0)})
        else:
            doc.modelspace().add_text(
                label, dxfattribs={"height": 5.0, "insert": (0, 0)}
            )
        doc.saveas(str(path))

    _write_minimal(d / "cleaned" / "a.dxf", "TP104-A", in_block=False)
    _write_minimal(d / "cleaned" / "b.dxf", "TP104-A", in_block=True)

    # Stage 3.5a: write the agreement file directly.
    cross = {
        "agreement_texts":     ["TP104-A"],
        "agreement_by_prefix": {"TP104-": [
            {"text": "TP104-A", "prefix": "TP104-", "suffix": "A",
             "container": "modelspace", "block_name": None,
             "position": [0, 0], "handle": "1", "drawing": "a.dxf"},
            {"text": "TP104-A", "prefix": "TP104-", "suffix": "A",
             "container": "GridLine-X", "block_name": "GridLine-X",
             "position": [0, 0], "handle": "2", "drawing": "b.dxf"},
        ]},
        "drawings_scanned":    ["a.dxf", "b.dxf"],
    }
    (d / "stages" / "cross_drawing_agreement.json").write_text(json.dumps(cross))

    clean_policy.run(job_id)
    pol = json.loads((d / "stages" / "clean_policy.json").read_text())
    assert "GridLine-" in pol["keep_block_prefixes_str"]
