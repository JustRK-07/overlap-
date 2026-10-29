#!/usr/bin/env python3
"""core/roles.py — explicit MECH / STR roles for every stage.

The single source of truth is the manifest ``jobs/<id>/stages/files.json``
(written by the convert stage). Roles come from the API (legacy
``drawing_a`` = MECH, ``drawing_b`` = STR; N-file uploads use ``roles``).
Nothing in the pipeline may infer a role from a filename or from sort
order.

Layout (step 1):
    uploads/<name>              raw upload
    converted/<stem>.dxf        "converted" input (output of stage 3)
    cleaned/<stem>_clean.dxf    cleaner output (stage 6)

Old job dirs have no manifest; ``entries_from_job`` rebuilds the same
information from ``job.json``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from jobs import job_dir, load_job

from . import units

ROLES = ("mech", "str", "other")
MANIFEST_NAME = "files.json"


class RoleError(RuntimeError):
    """Roles are missing or ambiguous (0 or >1 of a role)."""


@dataclass
class FileEntry:
    """One uploaded file. Paths are relative to the job dir."""
    file_id: str
    upload: str
    role: str
    converted: str
    cleaned: str
    insunits: int | None = None   # $INSUNITS recorded at clean time (step 3)

    @property
    def name(self) -> str:
        return Path(self.upload).name

    @property
    def converted_name(self) -> str:
        return Path(self.converted).name

    @property
    def cleaned_name(self) -> str:
        return Path(self.cleaned).name


# ─────────────────────────────────────────────────────────────────────────────
# Manifest I/O
# ─────────────────────────────────────────────────────────────────────────────

def manifest_path(job_id: str) -> Path:
    return job_dir(job_id) / "stages" / MANIFEST_NAME


def write_manifest(job_id: str, entries: list[FileEntry]) -> Path:
    p = manifest_path(job_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([asdict(e) for e in entries], indent=2))
    return p


def load_manifest(job_id: str) -> list[FileEntry] | None:
    p = manifest_path(job_id)
    if not p.exists():
        return None
    raw = json.loads(p.read_text())
    # Older manifests lack newer optional fields; take only known keys.
    return [FileEntry(**{k: e[k] for k in FileEntry.__dataclass_fields__ if k in e})
            for e in raw]


def record_insunits(job_id: str, file_id: str, insunits: int) -> None:
    """Store $INSUNITS on the manifest entry for ``file_id`` (no-op if no manifest)."""
    entries = load_manifest(job_id)
    if entries is None:
        return
    for e in entries:
        if e.file_id == file_id:
            e.insunits = int(insunits)
    write_manifest(job_id, entries)


# ─────────────────────────────────────────────────────────────────────────────
# Building entries from job.json
# ─────────────────────────────────────────────────────────────────────────────

_LEGACY_ROLES = {"drawing_a": "mech", "drawing_b": "str"}


def converted_rel(name: str) -> str:
    return f"converted/{Path(name).stem}.dxf"


def default_cleaned_rel(name: str) -> str:
    return f"cleaned/{Path(name).stem}_clean.dxf"


def locate_cleaned(directory: Path, name: str) -> str | None:
    """Find the cleaned DXF for upload `name` inside `directory`.

    Candidates, in order: ``<stem>_clean.dxf``; the upload itself when it
    is already named ``*_clean.dxf`` (pre-cleaned fixtures); and the
    legacy rename rule (strip a trailing ``_clean`` from the stem, re-add
    it). This looks up a *named* file, it never globs or sorts.
    """
    stem = Path(name).stem
    cands = [f"{stem}_clean.dxf"]
    if stem.endswith("_clean"):
        cands.append(f"{stem}.dxf")
        cands.append(f"{stem[: -len('_clean')]}_clean.dxf")
    for c in cands:
        if (directory / c).exists():
            return c
    return None


def _meta_fields(key: str, meta) -> tuple[str, str, str, str | None]:
    """(file_id, name, role, cleaned_filename) from a job.files value."""
    if isinstance(meta, dict):
        name = meta.get("name") or key
        role = meta.get("kind") or _LEGACY_ROLES.get(key, "other")
        cleaned = meta.get("cleaned_filename")
        fid = meta.get("file_id") or key
    else:
        name, role, cleaned, fid = meta.name, meta.kind, meta.cleaned_filename, meta.file_id
    if role not in ROLES:
        role = "other"
    return fid, name, role, cleaned


def entries_from_job(job) -> list[FileEntry]:
    """Build manifest entries from ``JobState.files`` (no disk scan of roles)."""
    d = job_dir(job.job_id)
    out: list[FileEntry] = []
    for key, meta in (job.files or {}).items():
        fid, name, role, cleaned = _meta_fields(key, meta)
        if cleaned:
            cleaned_rel = f"cleaned/{cleaned}"
        else:
            found = locate_cleaned(d / "cleaned", name)
            cleaned_rel = f"cleaned/{found}" if found else default_cleaned_rel(name)
        out.append(FileEntry(
            file_id=fid,
            upload=f"uploads/{Path(name).name}",
            role=role,
            converted=converted_rel(name),
            cleaned=cleaned_rel,
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Resolution
# ─────────────────────────────────────────────────────────────────────────────

def resolve_inputs(job_id: str) -> list[FileEntry]:
    """Entries for stages 3-6: manifest when present, else rebuilt from job.json."""
    entries = load_manifest(job_id)
    if entries is not None:
        return entries
    job = load_job(job_id)
    if job is None:
        raise RoleError(f"no such job: {job_id}")
    return entries_from_job(job)


def first_of_each(entries: list[FileEntry]) -> tuple[FileEntry | None, FileEntry | None]:
    """(first mech, first str) in manifest order (upload order, not name order)."""
    mech = next((e for e in entries if e.role == "mech"), None)
    strc = next((e for e in entries if e.role == "str"), None)
    return mech, strc


# ─────────────────────────────────────────────────────────────────────────────
# Units
# ─────────────────────────────────────────────────────────────────────────────

def _header_insunits(d: Path, entry: FileEntry) -> int | None:
    """The ``$INSUNITS`` of one file: the manifest's, else the file's own header.

    The manifest records it at clean time, so this is normally a dict
    lookup.  The file is only opened for a job whose manifest predates
    that (or was hand-written).
    """
    if entry.insunits is not None:
        try:
            return int(entry.insunits)
        except (TypeError, ValueError):
            return None
    for rel in (entry.cleaned, entry.converted):
        p = d / rel
        if not p.exists():
            continue
        try:
            from .dxfio import read_internal
            doc = read_internal(str(p))
            return int(doc.header.get("$INSUNITS", 0) or 0)
        except Exception:
            continue
    return None


def effective_units(job_id: str) -> dict[str, units.FileUnits]:
    """The units of every file of the job.

    The user's override wins over the ``$INSUNITS`` header — that is the
    whole point of the override.  Nothing here writes to a drawing: the
    answer lives in ``job.json`` and every stage reads it from there.
    """
    job = load_job(job_id)
    overrides = dict(getattr(job, "unit_overrides", None) or {}) if job else {}
    d = job_dir(job_id)
    out: dict[str, units.FileUnits] = {}
    try:
        entries = resolve_inputs(job_id)
    except Exception:
        entries = []
    for entry in entries:
        code = _header_insunits(d, entry)
        header_name = units.unit_name(code) if code else None
        override = units.normalise_unit(overrides.get(entry.file_id))
        chosen = override or header_name or "mm"
        if override:
            source = "override"
            reason = (f"the drawing's $INSUNITS header says "
                      f"{header_name or 'nothing'} but the units were set to "
                      f"{override} by hand")
        else:
            source = "header"
            reason = (f"the drawing's $INSUNITS header says {header_name}"
                      if header_name else
                      "the drawing declares no units; millimetres were assumed")
        out[entry.file_id] = units.FileUnits(
            file_id=entry.file_id,
            role=entry.role,
            name=chosen,
            header_code=code,
            header_name=header_name,
            override=override,
            source=source,
            reason=reason,
        )
    return out


def units_for_pair(job_id: str) -> tuple[units.FileUnits, units.FileUnits]:
    """(mech, str) effective units for the job's first MECH / first STR."""
    per_file = effective_units(job_id)
    entries = resolve_inputs(job_id)
    mech_e, str_e = first_of_each(entries)
    mech = per_file.get(mech_e.file_id) if mech_e else None
    strc = per_file.get(str_e.file_id) if str_e else None
    if mech is None:
        mech = units.FileUnits(role="mech")
    if strc is None:
        strc = units.FileUnits(role="str")
    return mech, strc


def _single(entries: list[FileEntry], role: str) -> FileEntry:
    found = [e for e in entries if e.role == role]
    if len(found) != 1:
        raise RoleError(
            f"expected exactly 1 {role.upper()} file; found {len(found)}"
            + (f": {[e.name for e in found]}" if found else "")
        )
    return found[0]


def _pair_from_json(pair_dir: Path) -> tuple[str, str] | None:
    root = pair_dir.parent
    pj = root / "pair.json"
    if pj.exists():
        d = json.loads(pj.read_text())
        return d["mech"], d["str"]
    md = root / "metadata.json"            # written after a pair run
    if md.exists():
        d = json.loads(md.read_text())
        if d.get("mech_file") and d.get("str_file"):
            return d["mech_file"], d["str_file"]
    return None


def _pair_converted_from_json(pair_dir: Path) -> tuple[str, str] | None:
    """The pair's *converted* source files, job-dir relative.

    ``pair.json`` gained ``mech_converted`` / ``str_converted`` (step 8b.5)
    so the mismatch stage can read the pre-cleaning drawings.  A pair
    directory written before that has no such key — the caller falls back
    to the cleaned files.
    """
    root = pair_dir.parent
    pj = root / "pair.json"
    if not pj.exists():
        return None
    try:
        d = json.loads(pj.read_text())
    except Exception:
        return None
    mech, strc = d.get("mech_converted"), d.get("str_converted")
    if mech and strc:
        return mech, strc
    return None


def resolve_pair(job_id: str, pair_dir: Path | None = None, *,
                 first: bool = False,
                 stage: str = "cleaned") -> tuple[Path, Path]:
    """Return (mech_path, str_path) for the given stage. Never sorts.

    * ``pair_dir`` set: read ``<pair_dir.parent>/pair.json`` ``{mech, str}``
      (falling back to ``metadata.json``, then to the job's roles looked up
      by name inside ``pair_dir``).
    * otherwise: the manifest's single mech + single str file.
      ``first=True`` relaxes "single" to "first of each" (legacy axes
      stages on N-file jobs).
    * ``stage`` is ``"cleaned"`` (the default — the pipeline's own output) or
      ``"converted"`` (the pre-cleaning drawings the mismatch stage reads).
    Raises RoleError for 0 / >1 of a role or missing files.
    """
    if stage not in ("cleaned", "converted"):
        raise RoleError(f"unknown stage {stage!r} — use 'cleaned' or 'converted'")
    if pair_dir is not None:
        pair_dir = Path(pair_dir)
        if stage == "converted":
            names = _pair_converted_from_json(pair_dir)
            if names is None:
                raise RoleError(
                    "this pair directory has no converted-file record "
                    "(pair.json lacks mech_converted/str_converted)")
            d = job_dir(job_id)
            mech, strc = d / names[0], d / names[1]
        else:
            names = _pair_from_json(pair_dir)
            if names is None:
                entries = resolve_inputs(job_id)
                mech_e, str_e = (first_of_each(entries) if first else
                                 (_single(entries, "mech"), _single(entries, "str")))
                if mech_e is None or str_e is None:
                    raise RoleError("job has no MECH and STR file")
                names = []
                for e in (mech_e, str_e):
                    n = (e.cleaned_name if (pair_dir / e.cleaned_name).exists()
                         else locate_cleaned(pair_dir, e.name))
                    names.append(n or e.cleaned_name)
                names = tuple(names)
            mech, strc = pair_dir / names[0], pair_dir / names[1]
    else:
        entries = resolve_inputs(job_id)
        if first:
            mech_e, str_e = first_of_each(entries)
            if mech_e is None or str_e is None:
                raise RoleError("job has no MECH and STR file")
        else:
            mech_e, str_e = _single(entries, "mech"), _single(entries, "str")
        d = job_dir(job_id)
        if stage == "converted":
            mech, strc = d / mech_e.converted, d / str_e.converted
        else:
            mech, strc = d / mech_e.cleaned, d / str_e.cleaned
            # Pre-cleaned/legacy layouts: locate by name when the planned
            # path is absent.
            for i, e in enumerate((mech_e, str_e)):
                p = (mech, strc)[i]
                if not p.exists():
                    alt = locate_cleaned(d / "cleaned", e.name)
                    if alt:
                        if i == 0:
                            mech = d / "cleaned" / alt
                        else:
                            strc = d / "cleaned" / alt
    for role, p in (("MECH", mech), ("STR", strc)):
        if not p.exists():
            raise RoleError(f"{role} {stage} DXF missing: {p}")
    return mech, strc


def write_pair_json(root: Path, mech_name: str, str_name: str, pair_id: str,
                    *, options: dict | None = None,
                    mech_converted: str | None = None,
                    str_converted: str | None = None) -> Path:
    """Write the pair's role manifest.

    ``{pair_id, mech, str, options?, mech_converted?, str_converted?}``

    ``options`` (step 9) records the stage tunables this pair runs with so
    the pair directory is self-describing afterwards.  ``mech_converted`` /
    ``str_converted`` (step 8b.5) are **job-dir relative** paths to the
    pre-cleaning drawings, so the mismatch stage can read them through
    :func:`resolve_pair` with ``stage="converted"``.
    """
    root.mkdir(parents=True, exist_ok=True)
    payload = {"pair_id": pair_id, "mech": mech_name, "str": str_name}
    if options:
        payload["options"] = dict(options)
    if mech_converted:
        payload["mech_converted"] = mech_converted
    if str_converted:
        payload["str_converted"] = str_converted
    p = root / "pair.json"
    p.write_text(json.dumps(payload, indent=2))
    return p
