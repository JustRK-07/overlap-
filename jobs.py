#!/usr/bin/env python3
"""jobs.py — job state on disk.

Each job is a UUID dir under JOBS_DIR/ containing:
  job.json         full state, written atomically
  status.json      lightweight, polled by frontend (subset of job.json)
  uploads/         raw user uploads
  cleaned/         cleaned DXFs after stage 4
  axes/            detected axes after stage 5
  transform.json   fitted affine after stage 6 (legacy single-pair)
  out/pairs/<id>/  per-pair artifacts (Slice-1 layout)
  out/             final deliverables after stages 7-8 (legacy)

State machine (job-level):
  queued → running → awaiting_axes (stage 5)
                        ↓ (user submits convention)
                      running → done
  any state → failed (with `error` field)

Pair-level state lives in `JobState.pairs[*].status` and is independent.

This module provides:
  - FileMeta + Pair + JobState dataclasses (the in-memory shape)
  - _migrate_legacy() — synthesises v2 fields from a v1 job.json
  - create_job()       — new N-file factory (Slice 2)
  - create_job_legacy() — back-compat shim for the 2-file upload endpoint
  - load_job() / save_job() / update_status()
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
JOBS_DIR = HERE / "jobs"
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_MUTEX = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Slice 2 — per-file + per-pair types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FileMeta:
    """One uploaded file.

    `file_id` is the stable identity used in `Pair.mech_file_id` and
    `Pair.str_file_id` so the pair list doesn't break if files are
    renamed mid-job.
    """
    file_id: str
    name: str
    size: int
    kind: str                                 # "mech" | "str" | "other"
    cleaned_filename: str | None = None       # populated by stage 4 (clean)


@dataclass
class Pair:
    """One (MECH, STR) pairing inside a job.

    Drives stages 6 → 8.5 scoped to a per-pair artifact root
    (`out/pairs/<pair_id>/`). Created at upload time (N-file endpoint)
    or synthesised at load time by `_migrate_legacy()`.
    """
    pair_id: str
    mech_file_id: str
    str_file_id: str
    status: str = "queued"                    # queued | running | done | failed
    strategy: str = "A"                       # "A" | "B" | "C"
    prefix: str = ""
    axes: list[str] = field(default_factory=list)
    enabled_checks: list[str] | None = None
    created_at: str = ""
    completed_at: str | None = None
    metrics: dict = field(default_factory=dict)   # mirrors metadata.json's metrics
    artifacts: dict = field(default_factory=dict) # mirrors metadata.json's artifacts
    error: str | None = None


@dataclass
class JobState:
    """Full job state — mirrors job.json on disk."""

    job_id: str
    created_at: str
    strategy: str = "A"                       # "A" | "B" | "C"  (job-level default; Pair.strategy overrides)
    clean_version: str = "v1"                 # "v1" (aggressive, default) | "v2" (less aggressive)

    state: str = "queued"                     # queued | running | awaiting_axes | done | failed
    current_step: int = 0                     # 1..9
    step_name: str = "Queued"
    message: str = "Waiting to start..."
    elapsed_sec: float = 0.0

    files: dict = field(default_factory=dict)            # file_id -> FileMeta (legacy keys drawing_a/drawing_b also supported as flat dicts)
    pairs: list[Pair] = field(default_factory=list)      # Slice 2 — every (MECH, STR) pairing in this job
    active_pair_id: str | None = None                    # legacy single-pair convenience; None when multi-pair
    legacy_v2: bool = False                              # True when _migrate_legacy() synthesised fields at load time

    stages: dict = field(default_factory=dict)            # per-stage output
    transform: dict | None = None                        # filled at stage 6 (legacy single-pair convenience)
    detected_axes: dict | None = None                    # filled at stage 5
    enabled_checks: list[str] = field(default_factory=list)  # spot-check toggles

    outputs: dict = field(default_factory=dict)           # {dxf_path, preview_png, zip, report} (legacy)
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Migration
# ─────────────────────────────────────────────────────────────────────────────

def _migrate_legacy(data: dict) -> dict:
    """Synthesise v2 fields from a v1 job.json in-place.

    Called by `load_job()` before dataclass validation. Handles three
    cases:

    1. v1 file shape `{drawing_a: {name,size}, drawing_b: {name,size}}`
       with no `pairs` → synthesises one Pair with pair_id="_legacy",
       file_ids equal to the legacy keys. Sets `legacy_v2=True` so the
       rest of the code can tell this was migrated (vs freshly
       uploaded with the v2 schema).

    2. v2 file shape `{<uuid>: {file_id, name, size, kind, ...}, ...}`
       with no `pairs` — leaves the files alone; the route that
       finished the upload is responsible for materialising pairs at
       creation time, but if some legacy write didn't, we synthesise
       one pair from the first mech + first str.

    3. Both `files` and `pairs` already present (newly uploaded v2
       job) → no-op.

    Returns the mutated dict so callers can chain.
    """
    pairs = data.get("pairs")
    files = data.get("files") or {}

    # Case 3 — nothing to do.
    if pairs:
        return data

    # Case 1 — v1 flat keys.
    if "drawing_a" in files and "drawing_b" in files:
        data["pairs"] = [{
            "pair_id":       "_legacy",
            "mech_file_id":  "drawing_a",
            "str_file_id":   "drawing_b",
            "status":        "done" if data.get("state") == "done" else "queued",
            "strategy":      data.get("strategy", "A"),
            "prefix":        "",
            "axes":          [],
            "enabled_checks": data.get("enabled_checks"),
            "created_at":    data.get("created_at", ""),
            "completed_at":  data.get("created_at") if data.get("state") == "done" else None,
            "metrics":       {},
            "artifacts":     {},
            "error":         None,
        }]
        data["active_pair_id"] = "_legacy"
        data["legacy_v2"] = True
        return data

    # Case 2 — v2 file shape but no pairs (shouldn't happen for new
    # jobs, but be defensive). Synthesise from the first mech + first
    # str by `kind`.
    mech_ids = [fid for fid, meta in files.items()
                if isinstance(meta, dict) and meta.get("kind") == "mech"]
    str_ids  = [fid for fid, meta in files.items()
                if isinstance(meta, dict) and meta.get("kind") == "str"]
    if mech_ids and str_ids:
        data["pairs"] = [{
            "pair_id":      f"{mech_ids[0]}_vs_{str_ids[0]}",
            "mech_file_id": mech_ids[0],
            "str_file_id":  str_ids[0],
            "status":       "done" if data.get("state") == "done" else "queued",
            "strategy":     data.get("strategy", "A"),
        }]
        data["active_pair_id"] = data["pairs"][0]["pair_id"]
        data["legacy_v2"] = True

    return data


# ─────────────────────────────────────────────────────────────────────────────
# Disk I/O
# ─────────────────────────────────────────────────────────────────────────────

def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically: write to temp then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, path)


def _lock_for(job_id: str) -> threading.Lock:
    """Per-job lock so concurrent updates don't race."""
    with _LOCKS_MUTEX:
        if job_id not in _LOCKS:
            _LOCKS[job_id] = threading.Lock()
        return _LOCKS[job_id]


def save_job(job: JobState) -> None:
    """Persist the full JobState to job.json."""
    with _lock_for(job.job_id):
        _atomic_write(job_dir(job.job_id) / "job.json", asdict(job))


def load_job(job_id: str) -> JobState | None:
    """Read job.json. Returns None if missing.

    Migration discipline:
      1. `_migrate_legacy()` synthesises v2 fields from a v1 flat
         `files` shape so all 114 on-disk jobs (66 with `color_policy`
         still attached) load cleanly under the new schema.
      2. Strips any unknown fields before passing to JobState so old
         job files (e.g. with `color_policy` from an earlier schema)
         load cleanly.
      3. Coerces list-of-dict `pairs` into `Pair` instances and v2-
         shaped `files` values into `FileMeta` instances (JSON has no
         dataclass representation).
    """
    path = job_dir(job_id) / "job.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    data = _migrate_legacy(data)
    valid_fields = {f.name for f in JobState.__dataclass_fields__.values()}
    filtered = {k: v for k, v in data.items() if k in valid_fields}
    # Coerce pairs: list of dicts → list of Pair instances.
    raw_pairs = filtered.get("pairs") or []
    filtered["pairs"] = [Pair(**p) if isinstance(p, dict) else p for p in raw_pairs]
    # Coerce v2-shaped files values (have file_id + kind) to FileMeta.
    # Legacy flat dicts (drawing_a/drawing_b) stay as plain dicts so
    # back-compat consumers keep working.
    raw_files = filtered.get("files") or {}
    coerced: dict = {}
    for fid, val in raw_files.items():
        if isinstance(val, dict) and "file_id" in val and "kind" in val:
            coerced[fid] = FileMeta(**val)
        else:
            coerced[fid] = val
    filtered["files"] = coerced
    return JobState(**filtered)


def update_status(
    job_id: str,
    *,
    state: str | None = None,
    current_step: int | None = None,
    step_name: str | None = None,
    message: str | None = None,
    elapsed_sec: float | None = None,
    error: str | None = None,
) -> None:
    """Patch the job.json in-place — atomic, locked."""
    job = load_job(job_id)
    if job is None:
        return
    if state is not None:
        job.state = state
    if current_step is not None:
        job.current_step = current_step
    if step_name is not None:
        job.step_name = step_name
    if message is not None:
        job.message = message
    if elapsed_sec is not None:
        job.elapsed_sec = elapsed_sec
    if error is not None:
        job.error = error
        job.state = "failed"
    save_job(job)


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def _new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def _ensure_dirs(d: Path) -> None:
    """Make sure every per-job subdir exists. Idempotent."""
    (d / "uploads").mkdir(parents=True, exist_ok=True)
    (d / "cleaned").mkdir(parents=True, exist_ok=True)
    (d / "axes").mkdir(parents=True, exist_ok=True)
    (d / "out").mkdir(parents=True, exist_ok=True)


def create_job(
    *,
    files: list[dict],
    pairs: list[dict] | None = None,
    strategy: str = "A",
    clean_version: str = "v1",
    enabled_checks: list[str] | None = None,
) -> JobState:
    """Create a new N-file job: dirs + initial state + Pair list.

    `files` is a list of `{name, size, kind}` dicts (kind ∈
    {"mech", "str", "other"}). A `file_id` (8-hex uuid) is generated
    per file and used as the key in `JobState.files`.

    `pairs` is an explicit list of `{mech_file_id, str_file_id,
    strategy?, prefix?, axes?, enabled_checks?}` dicts, or None — when
    None and exactly one MECH + one STR exist, we auto-create the
    MECH×STR pair. The cross-product is the caller's responsibility
    (the /upload endpoint computes it from the user's `roles[]` form
    field).

    Returns the JobState. The caller writes the uploads to
    `job_dir(job_id)/uploads/` and starts the worker thread.
    """
    import datetime

    if not files:
        raise ValueError("files must be a non-empty list")
    for f in files:
        if not isinstance(f, dict) or not {"name", "size", "kind"}.issubset(f):
            raise ValueError(
                f"each file entry needs name/size/kind; got {f!r}",
            )
        if f["kind"] not in ("mech", "str", "other"):
            raise ValueError(f"unknown kind {f['kind']!r} (expected mech/str/other)")

    job_id = _new_job_id()
    d = job_dir(job_id)
    _ensure_dirs(d)

    # Build files dict + auto-pair when only 1 mech + 1 str + no pairs given.
    file_dict: dict[str, FileMeta] = {}
    for f in files:
        fid = uuid.uuid4().hex[:8]
        file_dict[fid] = FileMeta(
            file_id=fid,
            name=str(f["name"]),
            size=int(f["size"]),
            kind=str(f["kind"]),
        )

    pair_list: list[Pair] = []
    if pairs is not None:
        # Explicit pair list — may be empty (the /upload route computes
        # the cross-product itself and passes [] to trigger auto-pair
        # below).
        for p in pairs:
            mech_fid = p["mech_file_id"]
            str_fid = p["str_file_id"]
            if mech_fid not in file_dict or str_fid not in file_dict:
                raise ValueError(
                    f"pair references unknown file_id: mech={mech_fid!r} str={str_fid!r}",
                )
            pair_list.append(Pair(
                pair_id=p.get("pair_id") or f"{mech_fid}_vs_{str_fid}",
                mech_file_id=mech_fid,
                str_file_id=str_fid,
                status="queued",
                strategy=p.get("strategy", strategy),
                prefix=p.get("prefix", ""),
                axes=list(p.get("axes", [])),
                enabled_checks=p.get("enabled_checks", enabled_checks),
                created_at=datetime.datetime.now().isoformat(timespec="seconds"),
            ))

    if not pair_list:
        # Auto-pair cross-product: every MECH × every STR.
        mech_ids = [fid for fid, m in file_dict.items() if m.kind == "mech"]
        str_ids  = [fid for fid, m in file_dict.items() if m.kind == "str"]
        for mech_fid in mech_ids:
            for str_fid in str_ids:
                pair_list.append(Pair(
                    pair_id=f"{mech_fid}_vs_{str_fid}",
                    mech_file_id=mech_fid,
                    str_file_id=str_fid,
                    status="queued",
                    strategy=strategy,
                    enabled_checks=enabled_checks,
                    created_at=datetime.datetime.now().isoformat(timespec="seconds"),
                ))

    job = JobState(
        job_id=job_id,
        created_at=datetime.datetime.now().isoformat(timespec="seconds"),
        strategy=strategy,
        clean_version=clean_version,
        state="queued",
        current_step=0,
        step_name="Queued",
        message="Waiting to start...",
        # asdict() needs FileMeta values to serialise; store the dataclass
        # directly. (Legacy readers see a flat dict — that's fine; asdict
        # turns each FileMeta into {"file_id": ..., "name": ...}.)
        files={fid: m for fid, m in file_dict.items()},
        pairs=pair_list,
        active_pair_id=pair_list[0].pair_id if pair_list else None,
        legacy_v2=False,
        enabled_checks=enabled_checks or [],
    )
    save_job(job)
    return job


def create_job_legacy(
    *,
    drawing_a_name: str,
    drawing_a_size: int,
    drawing_b_name: str,
    drawing_b_size: int,
    strategy: str = "A",
    clean_version: str = "v1",
    enabled_checks: list[str] | None = None,
) -> JobState:
    """Backwards-compat shim for the 2-file upload endpoint.

    Mirrors the legacy v1 factory exactly so the existing `POST ""`
    endpoint keeps producing a v1-shaped `files` dict (drawing_a /
    drawing_b). On the next `load_job()` call, `_migrate_legacy()`
    synthesises the v2 `pairs` field — but only for jobs read back from
    disk. Jobs freshly created via this shim get the v2 fields at
    create time too, just synthesised here rather than at load.
    """
    import datetime

    job_id = _new_job_id()
    d = job_dir(job_id)
    _ensure_dirs(d)

    pair_id = "_legacy"
    created_at = datetime.datetime.now().isoformat(timespec="seconds")
    legacy_pair = Pair(
        pair_id=pair_id,
        mech_file_id="drawing_a",
        str_file_id="drawing_b",
        status="queued",
        strategy=strategy,
        prefix="",
        axes=[],
        enabled_checks=enabled_checks,
        created_at=created_at,
    )

    job = JobState(
        job_id=job_id,
        created_at=created_at,
        strategy=strategy,
        clean_version=clean_version,
        state="queued",
        current_step=0,
        step_name="Queued",
        message="Waiting to start...",
        # Flat v1 shape — every existing consumer reads
        # files.drawing_a.name etc. and stays bit-equivalent.
        files={
            "drawing_a": {"name": drawing_a_name, "size": drawing_a_size},
            "drawing_b": {"name": drawing_b_name, "size": drawing_b_size},
        },
        pairs=[legacy_pair],
        active_pair_id=pair_id,
        legacy_v2=True,
        enabled_checks=enabled_checks or [],
    )
    save_job(job)
    return job
