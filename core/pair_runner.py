#!/usr/bin/env python3
"""core/pair_runner.py — Slice 1 multi-pair orchestrator.

Drives a single (MECH, STR) pair through stages 6 → 8.5 without
modifying any of those stages' internal logic: the only lever is the
new `pair_dir` kwarg each stage accepts.

Pipeline (per pair):
    1. Materialise the pair dir:
         out/pairs/<pair_id>/cleaned/<mech>.dxf
         out/pairs/<pair_id>/cleaned/<str>.dxf
       (No re-cleaning — the project-wide cleaning policy is the
       same for every pair.)
    2. Write a per-pair axes/convention.json.
    3. Run fit → mech_intra → str_intra → spotcheck → overlay →
       package → spotcheck_annotate, all scoped to the pair root.
    4. Write a metadata.json sidecar with the pair's identity + metrics.

The pair_id is derived from the two cleaned filenames via
`encode_pair_id()` and is safe to embed in URLs after sanitisation.

Why a dedicated module?
  - Keeps the existing stages untouched (each only had to accept one
    extra optional `pair_dir=None` kwarg).
  - Keeps the migration discipline at one choke-point: when Slice 2
    refactors `JobState` to a real `pairs: list[Pair]` field, only
    this orchestrator changes.

Public API:
  encode_pair_id(mech_filename, str_filename) -> str
  run_pair(job_id, *, mech_filename, str_filename, pair_id,
           prefix, axes, strategy="A",
           enabled_checks=None) -> dict
"""
from __future__ import annotations

import datetime
import json
import re
import shutil
from pathlib import Path

from jobs import job_dir


# ─────────────────────────────────────────────────────────────────────────────
# Encoding
# ─────────────────────────────────────────────────────────────────────────────

# Anything outside [A-Za-z0-9_.+-] gets replaced with `_` so the pair_id
# is safe in URLs and on Windows filesystems (no `..`, no slashes, no
# spaces). 44-char limit keeps paths short.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.+-]")


def _stem(filename: str) -> str:
    """`TP-104 MECH GA_clean3.dxf` → `TP-104 MECH GA`."""
    name = Path(filename).name
    stem = Path(name).stem  # strip extension
    if stem.endswith("_clean"):
        stem = stem[: -len("_clean")]
    return stem


def _sanitise(s: str) -> str:
    return _SAFE_CHARS.sub("_", s)


def encode_pair_id(mech_filename: str, str_filename: str) -> str:
    """Build a stable, sanitised pair_id from two cleaned DXF filenames.

    Format: "<mech_stem>__vs__<str_stem>". Both stems have the file
    extension and any trailing `_clean` stripped, and any non-safe
    character replaced with `_`.
    """
    return f"{_sanitise(_stem(mech_filename))}_vs_{_sanitise(_stem(str_filename))}"


def pair_root(job_id: str, pair_id: str) -> Path:
    """The root directory for one pair's artifacts.

    Layout (Slice-1):
        jobs/<id>/out/pairs/<pair_id>/
            cleaned/<mech>.dxf
            cleaned/<str>.dxf
            axes/convention.json
            transform.json
            stages/{mech,str}_intra.json
            stages/spotcheck.json
            out/superimposed.dxf
            out/preview.png
            out/report.json
            out/spotcheck.docx
            out/spotcheck.json
            out/superimposed_annotated.dxf
            metadata.json
    """
    return job_dir(job_id) / "out" / "pairs" / pair_id


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def run_pair(
    job_id: str,
    *,
    mech_filename: str,
    str_filename: str,
    pair_id: str,
    prefix: str,
    axes: list[str],
    strategy: str = "A",
    enabled_checks: list[str] | None = None,
) -> dict:
    """Run a single pair end-to-end (stages 6 → 8.5).

    Returns a JSON-serialisable dict with the response shape used by
    `POST /{job_id}/pairs`.

    Pre-conditions (all 400-able, checked by the caller):
      - mech_filename != str_filename
      - both files exist in jobs/<id>/cleaned/
      - prefix and axes are non-empty (else fall back to saved convention)

    Effects:
      - Copies the two cleaned DXFs into the pair dir.
      - Writes axes/convention.json (per-pair).
      - Runs each stage with pair_dir=<pair_root>/cleaned/.
      - Persists a metadata.json sidecar with metrics.
    """
    d = job_dir(job_id)
    src_cleaned = d / "cleaned"

    if not (src_cleaned / mech_filename).exists():
        raise FileNotFoundError(
            f"mech_file {mech_filename!r} not in {src_cleaned}"
        )
    if not (src_cleaned / str_filename).exists():
        raise FileNotFoundError(
            f"str_file {str_filename!r} not in {src_cleaned}"
        )

    root = pair_root(job_id, pair_id)
    pair_cleaned = root / "cleaned"
    pair_cleaned.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_cleaned / mech_filename, pair_cleaned / mech_filename)
    shutil.copyfile(src_cleaned / str_filename, pair_cleaned / str_filename)

    # Pre-create the per-pair stages/ and out/ directories. Each stage
    # expects to write into them but doesn't mkdir()-and-write atomically;
    # we make them up front so the first stage write doesn't FileNotFound.
    (root / "axes").mkdir(exist_ok=True)
    (root / "stages").mkdir(exist_ok=True)
    (root / "out").mkdir(exist_ok=True)

    # Per-pair convention.json (used by core/fit.run via pair_dir)
    (root / "axes" / "convention.json").write_text(json.dumps({
        "prefix": prefix,
        "axes": list(axes),
    }))

    # Stage invocation order — mirrors pipelines.resume_after_axes but
    # scoped to the pair root. Each stage accepts `pair_dir` and writes
    # its outputs under root.
    import core.fit as fit_mod
    import core.mech_intra as mech_intra_mod
    import core.str_intra as str_intra_mod
    import core.spotcheck as spotcheck_mod
    import core.overlay as overlay_mod
    import core.package as package_mod
    import core.spotcheck_annotate as spotcheck_annotate_mod

    # 6 — fit. Writes transform.json under root.
    fit_mod.run(job_id, pair_dir=pair_cleaned)

    # 7 — overlay (depends on transform). Writes out/superimposed.dxf.
    # `strategy` is per-pair (Slice 2) — overlay.run accepts it explicitly
    # so we don't mutate the job-level strategy field.
    overlay_mod.run(job_id, pair_dir=pair_cleaned, strategy=strategy)

    # 7.5 — split spotchecks into MECH intra, STR intra, cross, then
    # annotate the DXF. Order matches pipelines.resume_after_axes().
    mech_intra_mod.run(job_id, enabled_checks=enabled_checks, pair_dir=pair_cleaned)
    str_intra_mod.run(job_id, enabled_checks=enabled_checks, pair_dir=pair_cleaned)
    spotcheck_mod.run(job_id, enabled_checks=enabled_checks, pair_dir=pair_cleaned)
    spotcheck_annotate_mod.run(job_id, pair_dir=pair_cleaned)

    # 8 — package (preview PNG + report JSON). Reads transform + spotcheck
    # JSON under root; writes out/preview.png + out/report.json.
    package_mod.run(job_id, pair_dir=pair_cleaned)

    # Read metrics out of the artifacts. Tolerant to a missing artifact
    # (e.g. spotcheck suppressed because saddle count was zero).
    metrics: dict = {}
    transform_path = root / "transform.json"
    if transform_path.exists():
        t = json.loads(transform_path.read_text())
        metrics["rms_residual"] = t.get("rms_residual")
        metrics["max_residual"] = t.get("max_residual")
    sc_path = root / "stages" / "spotcheck.json"
    if sc_path.exists():
        sc = json.loads(sc_path.read_text())
        metrics["saddles_total"] = sc.get("saddles_total")
        metrics["saddles_supported"] = sc.get("saddles_supported")
        metrics["n_issues"] = len(sc.get("issues", []))

    metadata = {
        "pair_id": pair_id,
        "mech_file": mech_filename,
        "str_file": str_filename,
        "strategy": strategy,
        "prefix": prefix,
        "axes": list(axes),
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "completed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "metrics": metrics,
        "artifacts": {
            "transform":           str(root / "transform.json"),
            "spotcheck":           str(root / "stages" / "spotcheck.json"),
            "mech_intra":          str(root / "stages" / "mech_intra.json"),
            "str_intra":           str(root / "stages" / "str_intra.json"),
            "superimposed":        str(root / "out" / "superimposed.dxf"),
            "superimposed_annotated": str(root / "out" / "superimposed_annotated.dxf"),
            "preview":             str(root / "out" / "preview.png"),
            "report":              str(root / "out" / "report.json"),
            "spotcheck_docx":      str(root / "out" / "spotcheck.docx"),
            "spotcheck_json":      str(root / "out" / "spotcheck.json"),
        },
    }
    (root / "metadata.json").write_text(json.dumps(metadata, indent=2))

    return {
        "job_id": job_id,
        "pair_id": pair_id,
        "mech_file": mech_filename,
        "str_file": str_filename,
        "strategy": strategy,
        "prefix": prefix,
        "axes": list(axes),
        "status": "done",
        "artifacts": metadata["artifacts"],
        "metrics": metrics,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Slice 2 — JobState-aware multi-pair orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def _update_pair_status(job_id: str, pair_id: str, *,
                        status: str,
                        metrics: dict | None = None,
                        artifacts: dict | None = None,
                        error: str | None = None,
                        completed_at: str | None = None) -> None:
    """Patch one Pair inside JobState (atomic, locked via save_job)."""
    import datetime
    from jobs import load_job, save_job
    job = load_job(job_id)
    if job is None:
        return
    for pair in job.pairs:
        if pair.pair_id != pair_id:
            continue
        pair.status = status
        if metrics is not None:
            pair.metrics = dict(metrics)
        if artifacts is not None:
            pair.artifacts = dict(artifacts)
        if error is not None:
            pair.error = error
        if completed_at is not None:
            pair.completed_at = completed_at
        elif status == "done":
            pair.completed_at = datetime.datetime.now().isoformat(timespec="seconds")
        break
    save_job(job)


def run_pairs_for_job(
    job_id: str,
    *,
    pairs: list,
    prefix: str,
    axes: list[str],
    enabled_checks: list[str] | None = None,
    pair_root_resolver=None,
) -> list[dict]:
    """Run every Pair through stages 6 → 8.5 with status updates.

    `pairs` is a list of Pair dataclasses (from `JobState.pairs`).
    Each pair's `mech_file_id` / `str_file_id` are looked up in
    `JobState.files` to find the cleaned filenames to feed
    `run_pair()`. Status transitions: queued → running → done (or
    failed). A failed pair does NOT stop the others — mirrors current
    Spot-check non-fatal behaviour.

    Intended caller: `routers/api.py:upload_n_files`.
    """
    import datetime
    from jobs import load_job, save_job

    job = load_job(job_id)
    if job is None:
        raise FileNotFoundError(f"job {job_id!r} disappeared")

    # Resolve file_id → cleaned filename via JobState.files. For legacy
    # jobs the keys are 'drawing_a' / 'drawing_b' (flat dicts, not
    # FileMeta); we accept both shapes.
    def _cleaned_name(fid: str) -> str:
        meta = job.files.get(fid)
        if meta is None:
            raise FileNotFoundError(
                f"file_id {fid!r} not in JobState.files for job {job_id!r}",
            )
        if isinstance(meta, dict):
            return meta.get("name") or meta.get("cleaned_filename") or fid
        return meta.cleaned_filename or meta.name

    results: list[dict] = []
    for pair in pairs:
        pair_id = pair.pair_id
        mech_filename = _cleaned_name(pair.mech_file_id)
        str_filename  = _cleaned_name(pair.str_file_id)

        # Mark running.
        _update_pair_status(job_id, pair_id, status="running")

        try:
            result = run_pair(
                job_id,
                mech_filename=mech_filename,
                str_filename=str_filename,
                pair_id=pair_id,
                prefix=prefix,
                axes=list(axes),
                strategy=pair.strategy,
                enabled_checks=pair.enabled_checks if pair.enabled_checks else enabled_checks,
            )
            # Copy metrics/artifacts into the Pair so the UI sees them.
            _update_pair_status(
                job_id, pair_id,
                status="done",
                metrics=result.get("metrics", {}),
                artifacts=result.get("artifacts", {}),
                completed_at=datetime.datetime.now().isoformat(timespec="seconds"),
            )
            results.append({"pair_id": pair_id, "status": "done",
                            "artifacts": result["artifacts"], "metrics": result["metrics"]})
        except Exception as e:
            _update_pair_status(job_id, pair_id, status="failed", error=str(e))
            results.append({"pair_id": pair_id, "status": "failed", "error": str(e)})

    return results
