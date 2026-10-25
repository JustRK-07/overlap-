#!/usr/bin/env python3
"""routers/api.py — overlay API endpoints.

POST   /api/v1/overlay              create a job from 2 file uploads
POST   /api/v1/overlay/upload       create a job from N file uploads (roles[])
GET    /api/v1/overlay/{id}         full job state
GET    /api/v1/overlay/{id}/status  lightweight status (polled every 500ms)
GET    /api/v1/overlay/{id}/axes    suggested conventions + matched axes
POST   /api/v1/overlay/{id}/axes    submit a convention (re-scan + proceed)
POST   /api/v1/overlay/{id}/axes/confirm  confirm axes (worker resumes)
GET    /api/v1/overlay/{id}/download  stream final DXF
GET    /api/v1/overlay/{id}/preview.png        preview PNG (full extents)
GET    /api/v1/overlay/{id}/preview_grid.png   preview PNG (grid box + 10%)
GET    /api/v1/overlay/{id}/preview_meta.json  world → pixel map of preview.png
GET    /api/v1/overlay/{id}/report.json  aggregate job report
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from jobs import (create_job, create_job_legacy, load_job, job_dir,
                  normalize_options, save_job)
import pipelines

router = APIRouter(prefix="/api/v1/overlay", tags=["overlay"])

ALLOWED_EXTS = {".dwg", ".dxf"}
MAX_UPLOAD_MB = 200


# ─────────────────────────────────────────────────────────────────────────────
# Job options (PLAN.md step 9)
# ─────────────────────────────────────────────────────────────────────────────
#
# Five user-facing knobs, all validated here (400 on a bad value) and
# persisted verbatim in `job.options` (the shape lives in jobs.py —
# `DEFAULT_OPTIONS` + `normalize_options`). The two readers are:
#
#   core.fit.FitOptions.from_job   — flat keys `fit_model` / `allow_weak_fit`
#                                    (aliases `model` / `force`)
#   core.overlay.OverlayStyle      — flat keys, or the `options["overlay"]`
#                                    sub-dict, which wins over the flat ones
#
# See README.md §"Options".

COLOR_MODES = ("original", "contrast")
FIT_MODELS = ("similarity", "affine")
MAX_LINEWEIGHT = 211       # DXF lineweights are 1/100 mm; 211 = 2.11 mm

_TRUE = {"1", "true", "yes", "on", "t", "y"}
_FALSE = {"0", "false", "no", "off", "f", "n", ""}


def _parse_bool(value, default: bool, *, field: str) -> bool:
    """Form/JSON bool. Anything but a recognised token is a 400."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
    raise HTTPException(
        400,
        f"{field} must be a boolean (true/false, 1/0, on/off); got {value!r}",
    )


def _parse_lineweight(value, *, field: str = "str_lineweight") -> int | None:
    """An int in DXF's 1/100 mm range, or empty/None → None (unset)."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(
            400,
            f"{field} must be an integer number of hundredths of a mm "
            f"(0..{MAX_LINEWEIGHT}) or empty; got {value!r}",
        ) from None
    if not 0 <= n <= MAX_LINEWEIGHT:
        raise HTTPException(
            400,
            f"{field} must be 0..{MAX_LINEWEIGHT} (hundredths of a mm); got {n}",
        )
    return n


def resolve_options(
    *,
    color_mode: str = "original",
    fit_model: str = "similarity",
    allow_weak_fit: bool = False,
    str_lineweight=None,
    qa_grid: bool = True,
) -> dict:
    """Validate the five options and return the ``job.options`` shape.

    Raises ``HTTPException(400)`` with a message naming the offending
    field and listing what is accepted.
    """
    mode = (color_mode or "").strip().lower()
    if mode not in COLOR_MODES:
        raise HTTPException(
            400,
            f"color_mode must be one of {' | '.join(COLOR_MODES)}; "
            f"got {color_mode!r}",
        )
    model = (fit_model or "").strip().lower()
    if model not in FIT_MODELS:
        raise HTTPException(
            400,
            f"fit_model must be one of {' | '.join(FIT_MODELS)}; "
            f"got {fit_model!r}",
        )

    return normalize_options({
        "color_mode": mode,
        "fit_model": model,
        "allow_weak_fit": _parse_bool(allow_weak_fit, False, field="allow_weak_fit"),
        "str_lineweight": _parse_lineweight(str_lineweight),
        "qa_grid": _parse_bool(qa_grid, True, field="qa_grid"),
    })


def set_option(job, key: str, value) -> None:
    """Patch one option on a loaded job, keeping the namespaces in sync.

    Used by ``POST /{job_id}/axes/confirm`` for `force`, which arrives
    after the job was created (and can therefore not be a form field).
    """
    job.options = normalize_options({**(job.options or {}), key: value})


@router.post("")
async def create_overlay(
    drawing_a: UploadFile = File(..., alias="drawing_a"),
    drawing_b: UploadFile = File(..., alias="drawing_b"),
    clean_version: str = Form("v1"),
    enabled_checks: str = Form(""),
    color_mode: str = Form("original"),
    fit_model: str = Form("similarity"),
    # Declared `str`, not `bool`: FastAPI/pydantic would answer 422 for a
    # bad token, and the option contract here is a 400 that names the field.
    allow_weak_fit: str = Form("false"),
    str_lineweight: str = Form(""),
    qa_grid: str = Form("true"),
):
    """Upload 2 CAD files + options; spawn a worker; return job_id.

    `drawing_a` is the **MECH** drawing, `drawing_b` the **STR** one.
    Roles are explicit and never inferred from the filenames.
    """
    # Validate the tunables first — a bad option must not leave a job dir
    # behind for a request that is going to be rejected anyway.
    options = resolve_options(
        color_mode=color_mode,
        fit_model=fit_model,
        allow_weak_fit=allow_weak_fit,
        str_lineweight=str_lineweight,
        qa_grid=qa_grid,
    )

    # Validate clean_version
    if clean_version not in ("v1", "v2"):
        raise HTTPException(400, f"clean_version must be v1 or v2; got {clean_version!r}")

    # Parse enabled_checks: comma-separated, empty = all
    checks = [c.strip() for c in enabled_checks.split(",") if c.strip()]

    # Validate file types
    for f in (drawing_a, drawing_b):
        ext = Path(f.filename or "").suffix.lower()
        if ext not in ALLOWED_EXTS:
            raise HTTPException(
                400,
                f"Only .dwg or .dxf files accepted; got {f.filename!r} ({ext})",
            )

    # Both drawings are written to uploads/ by name; identical names would
    # overwrite each other and silently give MECH == STR.
    if Path(drawing_a.filename or "").name == Path(drawing_b.filename or "").name:
        raise HTTPException(
            400,
            "drawing_a (MECH) and drawing_b (STR) have the same filename "
            f"{drawing_a.filename!r}; rename one so the roles stay distinct",
        )

    # Validate sizes (FastAPI streams; we just check post-upload content length)
    body_limit = MAX_UPLOAD_MB * 1024 * 1024
    for f in (drawing_a, drawing_b):
        if f.size and f.size > body_limit:
            raise HTTPException(413, f"{f.filename} exceeds {MAX_UPLOAD_MB} MB limit")

    # Create job + persist uploads
    job = create_job_legacy(
        drawing_a_name=drawing_a.filename or "a.dxf",
        drawing_a_size=drawing_a.size or 0,
        drawing_b_name=drawing_b.filename or "b.dxf",
        drawing_b_size=drawing_b.size or 0,
        clean_version=clean_version,
        enabled_checks=checks,
        options=options,
    )

    d = job_dir(job.job_id) / "uploads"
    (d / drawing_a.filename).write_bytes(await drawing_a.read())
    (d / drawing_b.filename).write_bytes(await drawing_b.read())

    # Spawn pipeline worker thread — DON'T await it; let it run in background
    threading.Thread(
        target=pipelines.run_pipeline,
        args=(job.job_id,),
        daemon=True,
    ).start()

    return {
        "job_id": job.job_id,
        "clean_version": clean_version,
        "enabled_checks": checks,
        "options": options,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Slice 2 — N-file upload endpoint
# ─────────────────────────────────────────────────────────────────────────────

MAX_FILES_PER_JOB = 16


@router.post("/upload")
async def upload_n_files(
    files: list[UploadFile] = File(..., alias="files"),
    roles: str = Form(..., alias="roles"),       # comma-separated, parallel to files
    clean_version: str = Form("v1"),
    enabled_checks: str = Form(""),
    auto_run: bool = Form(True),
    prefix: str = Form(""),
    axes: str = Form(""),                      # space-separated, e.g. "A B C 1 2 3"
    color_mode: str = Form("original"),
    fit_model: str = Form("similarity"),
    allow_weak_fit: str = Form("false"),   # str, not bool — see create_overlay
    str_lineweight: str = Form(""),
    qa_grid: str = Form("true"),
):
    """Upload N CAD files with per-file roles (MECH / STR / other).

    The server auto-creates every MECH×STR combination as a Pair. If
    `auto_run=True` (default), every pair runs sequentially in a worker
    thread before returning — same UX as the 2-file endpoint but
    fan-out. Returns the job_id, file_count, pair_count, the resolved
    `options`, and a summary of each pair's status + metrics.

    Notes:
      - The legacy `POST ""` endpoint is preserved for the 2-file flow.
      - For now, `prefix`/`axes` are job-level (shared by every pair).
        Per-pair axes can be added later by extending the form fields.
      - The stage options (`color_mode`, `fit_model`, `allow_weak_fit`,
        `str_lineweight`, `qa_grid`) are job-level too — every pair runs
        with them.
    """
    # FastAPI form fields don't accept list[str] — split the comma-
    # separated `roles` string ourselves.
    role_list = [r.strip() for r in roles.split(",") if r.strip()]

    # Validate the tunables before anything touches disk.
    options = resolve_options(
        color_mode=color_mode,
        fit_model=fit_model,
        allow_weak_fit=allow_weak_fit,
        str_lineweight=str_lineweight,
        qa_grid=qa_grid,
    )

    # Validate counts.
    if len(files) != len(role_list):
        raise HTTPException(
            400,
            f"files/roles length mismatch ({len(files)} vs {len(role_list)})",
        )
    if not (1 <= len(files) <= MAX_FILES_PER_JOB):
        raise HTTPException(
            400,
            f"file count must be 1..{MAX_FILES_PER_JOB}; got {len(files)}",
        )

    # Validate clean_version.
    if clean_version not in ("v1", "v2"):
        raise HTTPException(400, f"clean_version must be v1 or v2; got {clean_version!r}")

    # Validate role tags + ext + size.
    for i, (f, role) in enumerate(zip(files, role_list)):
        if role not in ("mech", "str", "other"):
            raise HTTPException(400, f"role {i} must be mech/str/other; got {role!r}")
        ext = Path(f.filename or "").suffix.lower()
        if ext not in ALLOWED_EXTS:
            raise HTTPException(400, f"file {i} {f.filename!r}: bad extension {ext}")
    body_limit = MAX_UPLOAD_MB * 1024 * 1024
    for f in files:
        if f.size and f.size > body_limit:
            raise HTTPException(413, f"{f.filename} exceeds {MAX_UPLOAD_MB} MB")

    mech_count = sum(1 for r in role_list if r == "mech")
    str_count  = sum(1 for r in role_list if r == "str")
    if mech_count < 1:
        raise HTTPException(400, "at least one file must be role=mech")
    if str_count < 1:
        raise HTTPException(400, "at least one file must be role=str")

    # No duplicate filenames (the cleaner uses them as keys).
    names = [f.filename or f"file_{i}.dxf" for i, f in enumerate(files)]
    if len(set(names)) != len(names):
        raise HTTPException(400, "duplicate filenames in upload")

    checks = [c.strip() for c in enabled_checks.split(",") if c.strip()]
    axes_list = [a.strip() for a in axes.split() if a.strip()]
    if not prefix:
        prefix = ""     # pair_runner will fall back to axes/convention.json

    # Build the factory payload.
    payload_files = [
        {"name": nm, "size": f.size or 0, "kind": role}
        for f, role, nm in zip(files, role_list, names)
    ]

    # Auto-pair: cross-product of every MECH × every STR. Explicit
    # `pairs=[]` could override this later; for now always auto.
    job = create_job(
        files=payload_files,
        pairs=[],                  # forces auto-pair via create_job
        clean_version=clean_version,
        enabled_checks=checks,
        options=options,
    )

    # Persist uploads to jobs/<id>/uploads/.
    uploads_dir = job_dir(job.job_id) / "uploads"
    for f, nm in zip(files, names):
        (uploads_dir / nm).write_bytes(await f.read())

    # Build the file_id → cleaned-filename mapping so the legacy pipeline
    # (which reads from `cleaned/` after stage 4) finds files. We use the
    # original uploaded name; the cleaner preserves extensions.
    from jobs import save_job
    file_id_to_name = {fid: m.name for fid, m in job.files.items()}
    job.active_pair_id = job.pairs[0].pair_id if job.pairs else None
    save_job(job)

    # Spawn the legacy pipeline worker (stages 1-4 produce cleaned DXFs
    # under jobs/<id>/cleaned/). When it reaches `awaiting_axes`, we run
    # the multi-pair orchestrator instead of the legacy single-pair
    # resume_after_axes.
    pair_pipeline_done = threading.Event()

    def _worker() -> None:
        # Stage 1-4 — same as before.
        pipelines.run_pipeline(job.job_id)
        # Wait for user to submit a convention via /axes — we don't have
        # a UI step in the N-file flow, so we run the pipeline inline
        # here using the form-supplied prefix/axes. (If both empty, we
        # default to a generic 3x3 grid so things still work.)
        _maybe_run_pairs_after_cleaning(
            job.job_id,
            prefix=prefix or "TP-",
            axes=axes_list or ["A", "B", "C", "1", "2", "3"],
            pair_pipeline_done=pair_pipeline_done,
        )

    threading.Thread(target=_worker, daemon=True).start()

    if not auto_run:
        return {
            "job_id": job.job_id,
            "file_count": len(files),
            "pair_count": len(job.pairs),
            "pairs": [
                {"pair_id": p.pair_id, "status": p.status}
                for p in job.pairs
            ],
            "options": options,
            "auto_run": False,
            "message": "Pairs queued. Run them via POST /{id}/pairs or wait for the worker.",
        }

    # Wait for the worker to finish (caller can poll /status).
    # We return immediately with the queued pair list.
    return {
        "job_id": job.job_id,
        "file_count": len(files),
        "pair_count": len(job.pairs),
        "pairs": [
            {"pair_id": p.pair_id, "status": p.status}
            for p in job.pairs
        ],
        "options": options,
        "auto_run": True,
    }


def _maybe_run_pairs_after_cleaning(
    job_id: str, *, prefix: str, axes: list[str],
    pair_pipeline_done: threading.Event,
) -> None:
    """After the legacy pipeline finishes stages 1-4, run all pairs.

    Wraps `core.pair_runner.run_pairs_for_job` so the N-file upload
    endpoint stays thin.

    Filename reconciliation: the cleaner may rename an uploaded file
    (e.g. `TP-104 STR GA_clean_R12.dxf` →
    `TP-104 STR GA_clean_R12_clean.dxf`). Before running pairs, walk
    `jobs/<id>/cleaned/` and update each `FileMeta.cleaned_filename` to
    point at the actual disk file. This is what
    `run_pairs_for_job → run_pair` reads to copy DXFs into the pair dir.
    """
    from core.pair_runner import run_pairs_for_job
    from jobs import load_job, save_job, update_status

    job = load_job(job_id)
    if job is None:
        pair_pipeline_done.set()
        return
    if not job.pairs:
        update_status(job_id, state="failed", error="no pairs to run")
        pair_pipeline_done.set()
        return

    try:
        # Stage 5 — write axes/convention.json so any per-stage path that
        # expects it (e.g. core.fit) finds one.
        (job_dir(job_id) / "axes" / "convention.json").write_text(json.dumps({
            "prefix": prefix,
            "axes": list(axes),
        }))

        # FileMeta.cleaned_filename is written by the cleaner (stage 6).
        # Only for jobs whose cleaner predates that (or pre-cleaned
        # fixtures) look the file up by name via core.roles.
        from core.roles import locate_cleaned
        cleaned_dir = job_dir(job_id) / "cleaned"
        for fid, meta in job.files.items():
            current = meta.get("cleaned_filename") if isinstance(meta, dict) else meta.cleaned_filename
            if current and (cleaned_dir / current).exists():
                continue
            original = meta.get("name") if isinstance(meta, dict) else meta.name
            cleaned_name = locate_cleaned(cleaned_dir, original) or original
            if isinstance(meta, dict):
                meta["cleaned_filename"] = cleaned_name
            else:
                meta.cleaned_filename = cleaned_name
        save_job(job)

        update_status(job_id, state="running", current_step=6,
                      step_name="Running pairs",
                      message=f"Running {len(job.pairs)} pair(s)...")
        run_pairs_for_job(
            job_id,
            pairs=list(job.pairs),
            prefix=prefix,
            axes=list(axes),
            # The job-level options (color_mode / fit_model /
            # allow_weak_fit / str_lineweight / qa_grid) are job-wide —
            # every pair runs with them.
            options=job.options or None,
        )
        # Mark job done so the UI moves past section 2.
        update_status(job_id, state="done", current_step=9,
                      step_name="Done",
                      message=f"All {len(job.pairs)} pair(s) complete.")
    except Exception as e:
        update_status(job_id, error=f"pair run failed: {e}")
    finally:
        pair_pipeline_done.set()


@router.get("/{job_id}")
async def get_job(job_id: str):
    """Full job state."""
    job = load_job(job_id)
    if job is None:
        raise HTTPException(404, f"No such job: {job_id}")
    # Convert dataclass to dict
    from dataclasses import asdict
    return JSONResponse(content=asdict(job))


@router.get("/{job_id}/status")
async def get_status(job_id: str):
    """Lightweight status for polling."""
    job = load_job(job_id)
    if job is None:
        raise HTTPException(404, f"No such job: {job_id}")
    return {
        "job_id": job.job_id,
        "state": job.state,
        "current_step": job.current_step,
        "step_name": job.step_name,
        "message": job.message,
        "elapsed_sec": job.elapsed_sec,
        "error": job.error,
    }


# Stages 5+ (axes, fit, overlay, package, download) are wired here.
# The worker pauses at stage 5 (awaiting_axes) and the frontend polls /status,
# then POSTs to /axes + /axes/confirm to resume.


# ─────────────────────────────────────────────────────────────────────────────
# Stage 5 endpoints — axes picker
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{job_id}/axes")
async def get_axes(job_id: str):
    """Return auto-suggested grid conventions from both cleaned drawings.

    Also returns the auto-derived cleaning policy (label regex, block
    prefixes, marker layer, annotation layers) so the UI can pre-fill
    the Advanced panel.
    """
    job = load_job(job_id)
    if job is None:
        raise HTTPException(404, f"No such job: {job_id}")
    suggestions_path = job_dir(job_id) / "stages" / "axes_suggestions.json"
    if not suggestions_path.exists():
        try:
            from core import axes as axes_mod
            axes_mod.run(job_id)
        except Exception as e:
            raise HTTPException(500, f"Failed to suggest axes: {e}")
    payload = json.loads(suggestions_path.read_text())

    # Attach the auto-derived policy (if stage 3.5 has run)
    policy_path = job_dir(job_id) / "stages" / "clean_policy.json"
    if policy_path.exists():
        payload["clean_policy"] = json.loads(policy_path.read_text())
    return JSONResponse(content=payload)


@router.post("/{job_id}/policy")
async def save_policy(job_id: str, body: dict):
    """User overrode cleaning policy via the Advanced panel.

    Stores the overrides and writes them to stages/clean_policy.json so
    stage 4 (clean) reads them. Only allowed while state == awaiting_axes
    (before cleaning has run).
    """
    job = load_job(job_id)
    if job is None:
        raise HTTPException(404, f"No such job: {job_id}")
    if job.state != "awaiting_axes":
        raise HTTPException(
            400,
            f"Job is in state {job.state!r}; policy can only be edited "
            "before cleaning runs (during stage 5).",
        )

    # Pull the existing policy (must have been derived at stage 3.5) and
    # overlay the user's edits on top. This way, fields the user didn't
    # touch keep their auto-derived values.
    stages_dir = job_dir(job_id) / "stages"
    policy_path = stages_dir / "clean_policy.json"
    if not policy_path.exists():
        raise HTTPException(500, "No policy derived yet (stage 3.5 missing)")
    policy = json.loads(policy_path.read_text())

    # Field-by-field merge with type coercion for list fields
    def _merge(key, value_key):
        if value_key not in body:
            return
        v = body[value_key]
        if key in ("keep_block_prefixes_str",
                   "keep_block_prefixes_mech",
                   "annotation_layers",
                   "marker_block_prefixes",
                   "triangle_marker_layers",
                   "known_orphan_blocks"):
            if not isinstance(v, list):
                v = [x.strip() for x in str(v).split(",") if x.strip()]
            policy[key] = v
        else:
            policy[key] = v

    _merge("label_regex", "label_regex")
    _merge("keep_block_prefixes_str", "keep_block_prefixes_str")
    _merge("keep_block_prefixes_mech", "keep_block_prefixes_mech")
    _merge("marker_layer", "marker_layer")
    _merge("annotation_layers", "annotation_layers")
    _merge("marker_block_prefixes", "marker_block_prefixes")
    _merge("triangle_marker_layers", "triangle_marker_layers")
    _merge("known_orphan_blocks", "known_orphan_blocks")

    # Persist
    policy_path.write_text(json.dumps(policy, indent=2))

    # Also surface on job.json stages dict so the UI sees the latest values
    if job.stages is None:
        job.stages = {}
    job.stages["clean_policy"] = policy
    save_job(job)

    return {"ok": True, "clean_policy": policy}


_UNKNOWN_AXIS_STATUS = {
    "status": "missing",
    "reasons": ["not scanned"],
    "n_occurrences": 0,
}


def _scan_axes(path: Path, prefix: str, suffixes: list[str]):
    """(label occurrences, grid verdict per label) for one cleaned drawing.

    ``found`` is exactly what :func:`core.axes.find_axes` returns — the
    first TEXT/MTEXT/ATTRIB occurrence of ``prefix + suffix`` in world
    coordinates — so the legacy `pairs` deltas keep their meaning.
    ``status`` adds :func:`core.grid.detect_grid`'s verdict for the same
    suffix: which gridline (if any) the label's occurrences voted for.

    The document is read once. Grid detection is advisory: if it raises
    (an exotic DXF), the label positions still come back and the status
    falls back to "missing" rather than failing the request.
    """
    from core import grid
    from core.dxfio import read_internal

    try:
        doc = read_internal(str(path))
    except Exception as exc:                       # noqa: BLE001 — reported, not raised
        found = []
        status = {s: {**_UNKNOWN_AXIS_STATUS,
                      "reasons": [f"could not read the drawing: {exc}"]}
                  for s in suffixes}
        return found, status

    occs = grid.label_occurrences(doc, prefix, suffixes)
    found = [
        {"suffix": sfx,
         "type": "letter" if sfx.isalpha() else "number",
         "x": float(occs[sfx][0].world_pt.x),
         "y": float(occs[sfx][0].world_pt.y)}
        for sfx in suffixes if occs.get(sfx)
    ]

    try:
        result = grid.detect_grid(doc, prefix, suffixes)
    except Exception:                              # noqa: BLE001 — advisory only
        return found, {s: dict(_UNKNOWN_AXIS_STATUS) for s in suffixes}

    status = {}
    for sfx in suffixes:
        ax = result.axes.get(sfx)
        if ax is None:
            status[sfx] = {
                "status": "missing",
                "reasons": ["no label occurrence matches this prefix + suffix"],
                "n_occurrences": 0,
            }
            continue
        status[sfx] = {
            "status": ax.status,
            "reasons": list(ax.reasons),
            "n_occurrences": ax.n_occ,
            "n_agree": ax.n_agree,
            "point": None if ax.point is None else [ax.point.x, ax.point.y],
            "dir": None if ax.dir is None else [ax.dir.x, ax.dir.y],
            "length": ax.length,
        }
    return found, status


@router.post("/{job_id}/axes")
async def apply_axes(job_id: str, body: dict):
    """User submitted a convention {prefix, axes}. Re-scan and return matched axes.

    On top of the legacy `pairs` list (one entry per suffix found in BOTH
    drawings, with the axis-offset delta) the response carries a per-label
    `labels` array with the grid-detection verdict from
    :func:`core.grid.detect_grid` for each drawing — `"ok"`, `"ambiguous"`
    or `"missing"`, with the detector's reasons. That is what tells the
    user *why* an axis did not match, instead of just "4 / 7".
    """
    job = load_job(job_id)
    if job is None:
        raise HTTPException(404, f"No such job: {job_id}")
    if job.state != "awaiting_axes":
        raise HTTPException(400, f"Job is in state {job.state!r}, not awaiting_axes")

    prefix = body.get("prefix", "")
    axes = body.get("axes", [])
    if not isinstance(axes, list):
        raise HTTPException(400, "axes must be a list")

    # Locate cleaned DXFs by explicit role (first MECH, first STR).
    from core.roles import RoleError, resolve_pair
    try:
        mech_path, str_path = resolve_pair(job_id, first=True)
    except RoleError as e:
        raise HTTPException(500, str(e))

    # Match in each (a = MECH, b = STR) + the per-label grid verdict.
    found_a, status_a = _scan_axes(mech_path, prefix, axes)
    found_b, status_b = _scan_axes(str_path, prefix, axes)

    # Build paired list (matched by suffix)
    pairs = []
    for sfx in axes:
        a = next((x for x in found_a if x["suffix"] == sfx), None)
        b = next((x for x in found_b if x["suffix"] == sfx), None)
        if a and b:
            # Compute delta (axis is X for letters, Y for numbers)
            if a["type"] == "letter":
                delta = a["x"] - b["x"]
            else:
                delta = a["y"] - b["y"]
            pairs.append({
                "suffix": sfx,
                "type": a["type"],
                "a_pos": [a["x"], a["y"]],
                "b_pos": [b["x"], b["y"]],
                "delta": delta,
            })

    # Per-label status — every requested suffix appears, whether it matched
    # or not, so the UI can list what is missing instead of what isn't.
    labels = []
    problems = []
    for sfx in axes:
        sa = status_a.get(sfx) or _UNKNOWN_AXIS_STATUS
        sb = status_b.get(sfx) or _UNKNOWN_AXIS_STATUS
        a = next((x for x in found_a if x["suffix"] == sfx), None)
        b = next((x for x in found_b if x["suffix"] == sfx), None)
        matched = bool(a and b)
        entry = {
            "suffix": sfx,
            "type": "letter" if str(sfx).isalpha() else "number",
            "mech": sa,
            "str": sb,
            "matched": matched,
            "status": ("ok" if matched else
                       ("ambiguous" if "ambiguous" in (sa["status"], sb["status"])
                        else "missing")),
        }
        labels.append(entry)
        for source, st in (("mech", sa), ("str", sb)):
            if st["status"] != "ok":
                problems.append({
                    "suffix": sfx, "source": source,
                    "status": st["status"], "reasons": list(st.get("reasons") or []),
                })

    result = {
        "prefix": prefix,
        "axes": axes,
        "a_found": len(found_a),
        "b_found": len(found_b),
        "all_matched": len(pairs) == len(axes),
        "pairs": pairs,
        # ── added in step 9 ──
        "labels": labels,
        "all_ok": all(l["status"] == "ok" for l in labels),
        "problems": problems,
    }

    # Persist for fit.py to read
    (job_dir(job_id) / "axes" / "convention.json").write_text(json.dumps(result, indent=2))
    return JSONResponse(content=result)


@router.post("/{job_id}/axes/confirm")
async def confirm_axes(job_id: str, request: Request):
    """User confirmed the axis pairing. Resumes the pipeline (stages 6-9).

    Optional JSON body::

        {"force": true}

    ``force`` maps to :class:`core.fit.FitOptions`' ``force``, i.e. every
    fit quality gate (too few inliers, a bad leave-one-out residual, a
    scale that contradicts ``$INSUNITS``) becomes a warning and the fit
    is written with ``quality.status == "forced"``. It is the same knob
    as the upload form's `allow_weak_fit`; because it is also a sensible
    thing to flip *after* seeing the axes report, it can be set here too.

    The body is optional — a POST with no body keeps the job's options as
    they were. A non-JSON body is a 400 rather than a silent no-op.
    """
    job = load_job(job_id)
    if job is None:
        raise HTTPException(404, f"No such job: {job_id}")
    if job.state != "awaiting_axes":
        raise HTTPException(400, f"Job is in state {job.state!r}, not awaiting_axes")

    raw = await request.body()
    if raw.strip():
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"body must be JSON: {e}") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be a JSON object, e.g. {\"force\": true}")
        if "force" in body:
            force = _parse_bool(body["force"], False, field="force")
            # `force` and `allow_weak_fit` are aliases of the same knob in
            # core.fit; write both so a hand-edited job.json can't have
            # them disagree (FitOptions takes whichever key it finds
            # first, which would otherwise be a silent surprise).
            set_option(job, "force", force)
            set_option(job, "allow_weak_fit", force)
            save_job(job)

    # Spawn worker for stages 6-9
    threading.Thread(
        target=pipelines.resume_after_axes,
        args=(job_id,),
        daemon=True,
    ).start()
    return {"status": "resuming", "options": job.options}


@router.get("/{job_id}/download")
async def download(job_id: str):
    """Stream the final superimposed DXF. Stage 9 deliverable.

    Looks up the DXF in both layouts: legacy single-pair
    (`jobs/<id>/out/superimposed.dxf`) and Slice-1 single-pair
    (`jobs/<id>/out/pairs/<pid>/out/superimposed.dxf`).
    """
    legacy = job_dir(job_id) / "out" / "superimposed.dxf"
    slice1 = None
    pairs_root = job_dir(job_id) / "out" / "pairs"
    job = load_job(job_id)
    if pairs_root.exists() and job is not None:
        # The active pair, not whichever directory glob() lists first.
        active = job.active_pair_id or (job.pairs[0].pair_id if job.pairs else None)
        if active and (pairs_root / active / "out" / "superimposed.dxf").exists():
            slice1 = pairs_root / active / "out" / "superimposed.dxf"
    chosen = slice1 if slice1 else legacy
    if not chosen.exists():
        raise HTTPException(404, f"Overlay not ready: {legacy}")
    return FileResponse(
        str(chosen),
        filename="superimposed.dxf",
        media_type="application/dxf",
    )


def _serve(job_id: str, name: str, media_type: str):
    """Stream ``jobs/<id>/out/<name>``, 404 when the stage hasn't written it."""
    path = job_dir(job_id) / "out" / name
    if not path.exists():
        raise HTTPException(404, f"No {name} yet: {path}")
    return FileResponse(str(path), media_type=media_type)


@router.get("/{job_id}/preview.png")
async def preview(job_id: str):
    """Preview PNG on the FULL extents (stage 8 output) — nothing is cropped."""
    return _serve(job_id, "preview.png", "image/png")


@router.get("/{job_id}/preview_grid.png")
async def preview_grid(job_id: str):
    """Preview PNG cropped to the grid bbox + 10% — the legible close-up.

    `preview.png` shows everything, including the stray geometry the report
    lists as outliers; a drawing with one badly-placed entity therefore
    renders tiny. This is the same render over the grid box only.
    """
    return _serve(job_id, "preview_grid.png", "image/png")


@router.get("/{job_id}/preview_meta.json")
async def preview_meta(job_id: str):
    """World → pixel map of `preview.png`, so the UI can place markers."""
    return _serve(job_id, "preview_meta.json", "application/json")


@router.get("/{job_id}/report.json")
async def report(job_id: str):
    """Aggregate job report (stage 8 output)."""
    report_path = job_dir(job_id) / "out" / "report.json"
    if not report_path.exists():
        raise HTTPException(404, f"No report yet: {report_path}")
    return FileResponse(str(report_path), media_type="application/json")


# ─────────────────────────────────────────────────────────────────────────────
# Spot-check endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{job_id}/spotcheck.json")
async def spotcheck_json(job_id: str):
    """Spot-check results as JSON (issue list + tolerances)."""
    p = job_dir(job_id) / "out" / "spotcheck.json"
    if not p.exists():
        raise HTTPException(404, f"No spotcheck yet: {p}")
    return FileResponse(str(p), media_type="application/json")


@router.get("/{job_id}/spotcheck.docx")
async def spotcheck_docx(job_id: str):
    """Spot-check report as DOCX."""
    p = job_dir(job_id) / "out" / "spotcheck.docx"
    if not p.exists():
        raise HTTPException(404, f"No spotcheck docx yet: {p}")
    return FileResponse(
        str(p),
        filename="spotcheck.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@router.get("/{job_id}/annotated.dxf")
async def annotated_dxf(job_id: str):
    """Superimposed DXF with spot-check clouds + labels drawn on top."""
    p = job_dir(job_id) / "out" / "superimposed_annotated.dxf"
    if not p.exists():
        raise HTTPException(404, f"No annotated DXF yet: {p}")
    return FileResponse(
        str(p),
        filename="superimposed_annotated.dxf",
        media_type="application/dxf",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Slice 1 — Multi-pair endpoints
# ─────────────────────────────────────────────────────────────────────────────

from core.pair_runner import encode_pair_id, pair_root, run_pair


# ─────────────────────────────────────────────────────────────────────────────
# Slice 3 — Depth-analysis endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{job_id}/analyze")
async def analyze(job_id: str, pair_id: str | None = None):
    """Per-pair depth analysis + issues payload for the UI panel.

    Without `pair_id`, the response is either:
      • `{pairs: [...]}` — every pair (Slice-1 layout), OR
      • a single analyze payload — legacy single-pair job (artifacts
        live at `jobs/<id>/`, not under `out/pairs/`).

    With `pair_id=<id>`, the response is that pair's analyze payload
    only.
    """
    job = load_job(job_id)
    if job is None:
        raise HTTPException(404, f"No such job: {job_id}")

    from core.analyze import analyze_pair

    if pair_id is not None:
        try:
            return analyze_pair(job_id, pair_id=pair_id)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except Exception as e:
            raise HTTPException(500, f"analyze_pair failed: {e}")

    # pair_id is None — return the union (or the legacy single pair).
    pairs_root = job_dir(job_id) / "out" / "pairs"
    if pairs_root.exists():
        pair_ids = sorted(
            p.name for p in pairs_root.iterdir()
            if p.is_dir() and (p / "transform.json").exists()
        )
        out_pairs = []
        for pid in pair_ids:
            try:
                out_pairs.append(analyze_pair(job_id, pair_id=pid))
            except FileNotFoundError:
                continue  # half-finished pair — skip silently
            except Exception as e:
                # Don't fail the whole call if one pair misbehaves.
                out_pairs.append({
                    "pair_id": pid, "error": str(e),
                    "n_issues": 0, "intersections": [],
                    "hotspots": [], "bbox_diagonals": {},
                })
        return {"job_id": job_id, "pairs": out_pairs}

    # Legacy single-pair job.
    try:
        return analyze_pair(job_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"analyze_pair failed: {e}")


def _safe_pair_id(raw: str) -> str:
    """Sanitise a pair_id from a URL param to defeat `../` traversal.

    The router routes `POST /{job_id}/pairs` with a body-supplied
    pair_id (computed via `encode_pair_id`); the GET handlers below
    receive pair_id from the URL path, which is untrusted.
    """
    if not raw:
        raise HTTPException(400, "pair_id missing")
    # Block any kind of path-component traversal.
    if "/" in raw or "\\" in raw or raw in (".", ".."):
        raise HTTPException(400, f"pair_id must not contain path separators: {raw!r}")
    # Sanitise to the same charset encode_pair_id produces.
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9_.+-]", "_", raw)
    if not safe or safe != raw:
        raise HTTPException(400, f"pair_id has unsafe characters: {raw!r}")
    return safe


@router.post("/{job_id}/pairs")
async def create_pair(job_id: str, body: dict):
    """Run a (MECH, STR) pair through stages 6 → 8.5 and persist artifacts
    under `out/pairs/<pair_id>/`.

    Body:
        {
          "mech_file": "TP-104 MECH GA_clean3.dxf",
          "str_file":  "TP-104 STR GA_clean3.dxf",
          "prefix":    "TP104-",
          "axes":      ["A","B","C","D","1","2","3"],
          "enabled_checks": null | [...],
          "options":   { ... }          // optional per-run override
        }

    `options` (optional) overrides the job's stage tunables for this pair
    only — see `resolve_options()` for the accepted keys. Without it the
    job's `job.options` (set at upload time) applies.

    If `prefix` / `axes` are missing we fall back to the saved
    `axes/convention.json` (the user's choices from the legacy flow).

    Returns the response shape from `run_pair`, including paths to all
    generated artifacts and the pair's metrics.
    """
    job = load_job(job_id)
    if job is None:
        raise HTTPException(404, f"No such job: {job_id}")
    if job.state == "failed":
        raise HTTPException(409, f"Job is failed; cannot run pair")

    mech_filename = body.get("mech_file")
    str_filename = body.get("str_file")
    if not mech_filename or not str_filename:
        raise HTTPException(400, "mech_file and str_file are required")
    if mech_filename == str_filename:
        raise HTTPException(400, "mech_file and str_file must differ")

    cleaned_dir = job_dir(job_id) / "cleaned"
    if not (cleaned_dir / mech_filename).exists():
        raise HTTPException(400, f"mech_file {mech_filename!r} not in cleaned/")
    if not (cleaned_dir / str_filename).exists():
        raise HTTPException(400, f"str_file {str_filename!r} not in cleaned/")

    prefix = body.get("prefix")
    axes = body.get("axes")
    if not prefix or not axes:
        # Fall back to the saved convention from the legacy flow.
        conv_path = job_dir(job_id) / "axes" / "convention.json"
        if conv_path.exists():
            conv = json.loads(conv_path.read_text())
            prefix = prefix or conv.get("prefix")
            axes = axes or conv.get("axes")
    if not prefix or not axes:
        raise HTTPException(
            400,
            "prefix and axes are required (not in saved convention either)",
        )

    pair_id = encode_pair_id(mech_filename, str_filename)

    enabled_checks = body.get("enabled_checks")
    if isinstance(enabled_checks, str):
        enabled_checks = [c.strip() for c in enabled_checks.split(",") if c.strip()]

    options = body.get("options")
    if options is not None and not isinstance(options, dict):
        raise HTTPException(400, "options must be a JSON object")

    try:
        result = run_pair(
            job_id,
            mech_filename=mech_filename,
            str_filename=str_filename,
            pair_id=pair_id,
            prefix=prefix,
            axes=list(axes),
            enabled_checks=enabled_checks if enabled_checks else None,
            options=options,
        )
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"run_pair failed: {e}")

    return result


@router.get("/{job_id}/pairs")
async def list_pairs(job_id: str):
    """List every pair this job has run, with metrics and artifact presence."""
    job = load_job(job_id)
    if job is None:
        raise HTTPException(404, f"No such job: {job_id}")

    pairs_root = job_dir(job_id) / "out" / "pairs"
    if not pairs_root.exists():
        return {"job_id": job_id, "pairs": []}

    # Build a quick lookup from job.pairs by pair_id so we can attach
    # the live `status` to each entry (metadata.json is only written
    # on completion and doesn't carry status).
    status_by_pair = {p.pair_id: p.status for p in job.pairs}

    out = []
    for pair_dir in sorted(pairs_root.iterdir()):
        if not pair_dir.is_dir():
            continue
        meta_path = pair_dir / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        pair_id = meta.get("pair_id", pair_dir.name)
        out.append({
            "pair_id": pair_id,
            "mech_file": meta.get("mech_file"),
            "str_file": meta.get("str_file"),
            "status": status_by_pair.get(pair_id, "done"),
            "created_at": meta.get("created_at"),
            "completed_at": meta.get("completed_at"),
            "metrics": meta.get("metrics", {}),
            "artifacts": {
                k: v for k, v in (meta.get("artifacts") or {}).items()
                if Path(v).exists()
            },
        })

    return {"job_id": job_id, "pairs": out}


@router.get("/{job_id}/pairs/{pair_id}/{artifact}")
async def get_pair_artifact(job_id: str, pair_id: str, artifact: str):
    """Stream one artifact from a pair directory.

    `artifact` is one of:
      transform.json, spotcheck.json, mech_intra.json, str_intra.json,
      superimposed.dxf, superimposed_annotated.dxf, preview.png,
      preview_grid.png, preview_meta.json, report.json, spotcheck.docx,
      metadata.json

    Anything else → 400. `..` traversal → 400.
    """
    safe_id = _safe_pair_id(pair_id)

    # Map of (artifact_name) -> (media_type, subdir). The `subdir` is
    # either None (artifact lives directly under <pair_root>) or "out"
    # (artifact lives under <pair_root>/out/). The pair_runner writes
    # `transform.json`, `metadata.json`, `*_intra.json`, and
    # `axes/convention.json` at the pair root, while `superimposed.dxf`,
    # `superimposed_annotated.dxf`, `preview.png`, `preview_grid.png`,
    # `preview_meta.json`, `report.json`, `spotcheck.json`,
    # `spotcheck.docx` all land under `out/`.
    valid = {
        "transform.json":              ("application/json", None),
        "metadata.json":               ("application/json", None),
        "mech_intra.json":             ("application/json", None),
        "str_intra.json":              ("application/json", None),
        "spotcheck.json":              ("application/json", "out"),
        "report.json":                 ("application/json", "out"),
        "superimposed.dxf":            ("application/dxf", "out"),
        "superimposed_annotated.dxf":  ("application/dxf", "out"),
        "preview.png":                 ("image/png", "out"),
        "preview_grid.png":            ("image/png", "out"),
        "preview_meta.json":           ("application/json", "out"),
        "spotcheck.docx":              (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "out",
        ),
        "findings.json":               ("application/json", "out"),
        "findings.csv":                ("text/csv", "out"),
        "findings.docx":               (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "out",
        ),
        "mismatch.json":               ("application/json", "stages"),
    }
    if artifact not in valid:
        raise HTTPException(
            400,
            f"Unknown artifact {artifact!r}. Valid: {sorted(valid)}",
        )

    media, subdir = valid[artifact]
    base = pair_root(job_id, safe_id)
    path = base / subdir / artifact if subdir else base / artifact
    if not path.exists():
        raise HTTPException(404, f"No such pair artifact: {path}")

    download_name = artifact
    if download_name.startswith("superimposed") and download_name.endswith(".dxf"):
        download_name = f"superimposed_{safe_id}.dxf"
    elif download_name.startswith("preview"):
        # preview.png, preview_grid.png — a pair's preview is useless
        # without knowing which pair it came from.
        download_name = f"{Path(download_name).stem}_{safe_id}{Path(download_name).suffix}"

    return FileResponse(
        str(path),
        filename=download_name,
        media_type=media,
    )