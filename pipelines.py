#!/usr/bin/env python3
"""pipelines.py — drives the overlay stages.

The worker thread that runs after a job is created calls run_pipeline().
That function walks stages 1-6 and then pauses at stage 7 until the user
submits their grid convention via /axes. /axes/confirm calls
resume_after_axes(), which walks stages 8-11 to completion.

Each stage writes its output to jobs/<id>/<subdir>/ before the worker
moves on. update_status() reflects each stage's completion in job.json.
The UI's step rail numbers them the same way (README.md has the table).

Stages:
  1.  validate       — sniff formats, ensure files are readable
  2.  inspect        — count entities + layers (no render in MVP)
  3.  convert        — DWG → DXF via ezdxf.addons.odafc (skip if DXF input)
                       → converted/<stem>.dxf, and writes the roles manifest
  4.  labels         — scan both drawings for grid labels before cleaning
  5.  clean_policy   — derive the project-specific cleaning policy
  6.  clean          — strip annotations/markers (clean_v1 or clean_v2)
                       → cleaned/<stem>_clean.dxf
  7.  axes           — AUTO-SCAN + WAIT for the user's convention
  8.  fit            — similarity transform (Umeyama + RANSAC) + quality gates
  9.  overlay        — import STR via xref, transform, repaint per-source layers
  9.4 mismatch       — MECH⇄STR item-level misalignment (bolts, supports,
                       containment, clash); non-fatal
  9.5 spot-check     — support/clash issues; non-fatal
  9.6 findings       — merge the native findings with the remaining
                       spot-check issues into out/findings.{json,csv,docx}
  10. package        — preview PNG, preview grid PNG, report JSON
  11. done           — final marker
"""

from __future__ import annotations

import importlib
import json
import threading
import time
from pathlib import Path

from jobs import job_dir, load_job, save_job, update_status


def run_pipeline(job_id: str) -> None:
    """Run stages 1-6. Pause at stage 7 awaiting the user's convention."""
    job = load_job(job_id)
    if job is None:
        return

    started = time.time()
    clean_version = job.clean_version or "v1"
    stage4_module = f"clean_{clean_version}"

    for stage_num, stage_name, stage_module in [
        (1, "Validating", "validate"),
        (2, "Inspecting", "inspect"),
        (3, "Converting", "convert"),
        # Stage 4 — label-first: scan both drawings' labels BEFORE the
        # cleaner decides what to strip. Outputs protected_handles.json
        # + protected_blocks.json + cross_drawing_agreement.json so the
        # cleaner (stage 6) preserves any label present in both drawings.
        (4, "Scanning grid labels", "labels"),
        # Stage 5 — derive a project-specific cleaning policy. Uses
        # the agreement set produced by stage 4 to augment the
        # keep-block-prefixes list. The cleaner reads this in stage 6
        # to know which TEXT labels to keep, which INSERT blocks to
        # preserve, etc.
        (5, "Deriving cleaning policy", "clean_policy"),
        (6, f"Cleaning ({clean_version})", stage4_module),
    ]:
        update_status(job_id, state="running", current_step=stage_num,
                      step_name=stage_name,
                      message=f"{stage_name}...")
        try:
            _run_stage_module(stage_module, job_id)
            elapsed = time.time() - started
            update_status(job_id, elapsed_sec=elapsed, message=f"{stage_name} complete.")
        except Exception as e:
            update_status(job_id, message=f"{stage_name} failed: {e}", error=str(e))
            return

    # Stage 7 — axes: wait for user.
    update_status(job_id, state="awaiting_axes", current_step=7,
                  step_name="Picking grid convention",
                  message="Awaiting your grid naming convention...")


def resume_after_axes(job_id: str, *, pair_id: str | None = None) -> None:
    """Called by /axes/confirm after the user submits a convention.

    Walks stages 8-11 to completion. `pair_id=None` is the legacy
    single-pair path (writes to `jobs/<id>/` directly). When `pair_id`
    is given, the stages are scoped to that pair's root
    (`jobs/<id>/out/pairs/<pair_id>/`) via the `pair_dir` kwarg each
    stage module accepts.
    """
    started = time.time()

    # Resolve the pair (if any).
    job = load_job(job_id)
    pair = None
    pair_dir_arg = None
    if pair_id is not None and job is not None:
        for p in job.pairs:
            if p.pair_id == pair_id:
                pair = p
                break
        if pair is None:
            update_status(job_id, error=f"pair {pair_id!r} not found")
            return
        from core.pair_runner import pair_root
        pair_dir_arg = pair_root(job_id, pair_id) / "cleaned"

    # Stage 8 — fit.
    update_status(job_id, state="running", current_step=8,
                  step_name="Fitting",
                  message="Solving least-squares affine...")
    try:
        _run_stage_module("fit", job_id, pair_dir=pair_dir_arg)
        update_status(job_id, message="Affine transform solved.")
    except Exception as e:
        update_status(job_id, message=f"Fit failed: {e}", error=str(e))
        return

    # Stage 9 — overlay.
    update_status(job_id, state="running", current_step=9,
                  step_name="Building overlay",
                  message="Applying transform...")
    try:
        _run_stage_module("overlay", job_id, pair_dir=pair_dir_arg)
        update_status(job_id, message="Overlay built.")
    except Exception as e:
        update_status(job_id, message=f"Overlay failed: {e}", error=str(e))
        return

    # Stage 9.4 — mismatch (MECH⇄STR item-level misalignment).
    # NON-FATAL: the stage writes findings.json status="error" itself, so
    # this is only a backstop — the UI must never show "0 findings" for a
    # check that did not run.
    update_status(job_id, state="running", current_step=9,
                  step_name="Checking MECH/STR alignment",
                  message="Detecting MECH/STR mismatches...")
    try:
        _run_stage_module("mismatch", job_id, pair_dir=pair_dir_arg)
        update_status(job_id, message="Mismatch check complete.")
    except Exception as e:
        update_status(job_id, message=f"Mismatch check failed: {e}")

    # Stage 9.5 — spotcheck (auto-detect MECH↔STR support issues + intra clashes).
    update_status(job_id, state="running", current_step=9,
                  step_name="Spot-checking support",
                  message="Detecting equipment/structure clashes...")
    try:
        # Load enabled_checks from job — default = all
        enabled_checks = (job.enabled_checks if job and job.enabled_checks
                          else None)

        # 7.5a — MECH intra (skipped if no MECH-intra check is enabled)
        mech_intra_types = {"EQUIPMENT_OVERLAP", "BUILDING_CLASH", "CLEARANCE_VIOLATION"}
        if enabled_checks is None or any(t in enabled_checks for t in mech_intra_types):
            try:
                import core.mech_intra as mech_intra_mod
                mech_intra_mod.run(job_id,
                                   enabled_checks=[t for t in enabled_checks
                                                   if t in mech_intra_types]
                                                   if enabled_checks
                                                   else None,
                                   pair_dir=pair_dir_arg)
            except Exception as e:
                update_status(job_id, message=f"MECH-intra skipped: {e}")

        # 7.5b — STR intra
        str_intra_types = {"BEAM_COLUMN_CLASH", "BEAM_BEAM_CROSSING"}
        if enabled_checks is None or any(t in enabled_checks for t in str_intra_types):
            try:
                import core.str_intra as str_intra_mod
                str_intra_mod.run(job_id,
                                  enabled_checks=[t for t in enabled_checks
                                                  if t in str_intra_types]
                                                  if enabled_checks
                                                  else None,
                                  pair_dir=pair_dir_arg)
            except Exception as e:
                update_status(job_id, message=f"STR-intra skipped: {e}")

        # 7.5c — cross-drawing (merges everything into spotcheck.json).
        # Its `dedupe_against` defaults to stages/mismatch.json, so a
        # cross-drawing issue the mismatch engine already reported is
        # moved to `superseded` instead of being reported twice.
        _run_stage_module("spotcheck", job_id,
                          enabled_checks=enabled_checks, pair_dir=pair_dir_arg)
        # 7.5d — merge the native findings with the remaining spot-check
        # issues into the canonical out/findings.{json,csv,docx}.
        _run_stage_module("findings", job_id, pair_dir=pair_dir_arg)
        _run_stage_module("spotcheck_annotate", job_id, pair_dir=pair_dir_arg)
        update_status(job_id, message="Spot-check complete.")
    except Exception as e:
        # Spot-check is non-fatal — log but continue.
        update_status(job_id, message=f"Spot-check skipped: {e}")

    # Stage 10 — package (preview + report).
    update_status(job_id, state="running", current_step=10,
                  step_name="Packaging",
                  message="Rendering preview, writing report...")
    try:
        _run_stage_module("package", job_id, pair_dir=pair_dir_arg)
        update_status(job_id, message="Package ready.")
    except Exception as e:
        update_status(job_id, message=f"Package failed: {e}", error=str(e))
        return

    # Stage 11 — done. Pull the fresh transform.json + output paths into
    # job.json so the frontend can display the affine and download URL.
    _persist_final_outputs(job_id, pair_id=pair_id)
    elapsed = time.time() - started
    update_status(job_id, state="done", current_step=11,
                  step_name="Done",
                  message=f"Done in {elapsed:.1f}s. Ready to download.",
                  elapsed_sec=elapsed)


def _persist_final_outputs(job_id: str, *, pair_id: str | None = None) -> None:
    """Pull transform.json + out/superimposed.dxf paths into job.json.

    `pair_id=None` (legacy): artifacts live at `jobs/<id>/`. With
    `pair_id`: artifacts live at `jobs/<id>/out/pairs/<pair_id>/`.
    """
    job = load_job(job_id)
    if job is None:
        return
    if pair_id is not None:
        from core.pair_runner import pair_root
        d = pair_root(job_id, pair_id)
    else:
        d = job_dir(job_id)
    transform_path = d / "transform.json"
    if transform_path.exists():
        job.transform = json.loads(transform_path.read_text())
    out_path = d / "out" / "superimposed.dxf"
    if out_path.exists():
        job.outputs["dxf_path"] = str(out_path)
    save_job(job)


# ─────────────────────────────────────────────────────────────────────────────
# Stage driver
# ─────────────────────────────────────────────────────────────────────────────

def _run_stage_module(stage_name: str, job_id: str, **kwargs) -> None:
    """Dynamically import core.<stage_name> and call run(job_id, **kwargs)."""
    mod = importlib.import_module(f"core.{stage_name}")
    if not hasattr(mod, "run"):
        raise RuntimeError(f"core.{stage_name} has no run() function")
    mod.run(job_id, **kwargs)
