"""core.mismatch — MECH ⇄ STR mismatch detection (PLAN_MISMATCH step 8b).

``detect(mech_doc, str_doc, affine=None, *, policy=None, fit_ctx=None, …)``
    the whole of sub-step 8b.4: extract both drawings into features, classify
    them, run the four checks, apply the §3 local/systematic analysis, and
    return a ranked :class:`MismatchResult`.
``run(job_id, *, pair_dir=None)``
    lands in 8b.5; ``detect`` is deliberately independent of it so the checks
    can be exercised against a pair of files with no job, no policy file and
    no output directory at all.

The modules underneath:

``model``
    :class:`~core.mismatch.model.Finding`, :class:`~core.mismatch.model.EntityRef`,
    the kind/severity vocabularies, deterministic ranking and serialisers.
``policy``
    :class:`~core.mismatch.policy.MismatchPolicy`, ``DEFAULT_POLICY`` and
    :func:`~core.mismatch.policy.load_policy`.
``features`` / ``classify``
    per-block feature extraction and family assignment (8b.3).
``checks`` (bolts, support, containment, clash) and ``context``
    the checks themselves and the fit-uncertainty / local-vs-systematic
    analysis (8b.4).

Importing this package stays cheap and side-effect free; the heavy modules
are imported inside :func:`detect` so ``from core.mismatch import model`` does
not drag ezdxf's feature extractor along with it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "detect",
    "run",
    "MismatchResult",
    "DEFAULT_POLICY",
    "MismatchPolicy",
    "Classifier",
    "Finding",
    "EntityRef",
    "FitContext",
    "load_policy",
    "rank_findings",
    "summarize",
    "truncate",
    "model",
    "policy",
]


def __getattr__(name: str):
    """Lazy re-exports — ``core.mismatch.model`` costs nothing to import."""
    if name in ("model", "policy", "classify", "features", "geom", "context", "checks"):
        import importlib
        return importlib.import_module(f"{__name__}.{name}")
    if name in ("DEFAULT_POLICY", "MismatchPolicy", "load_policy"):
        from . import policy
        return getattr(policy, name)
    if name in ("Finding", "EntityRef", "rank_findings", "summarize", "truncate"):
        from . import model
        return getattr(model, name)
    if name == "Classifier":
        from .classify import Classifier
        return Classifier
    if name == "FitContext":
        from .context import FitContext
        return FitContext
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass
class MismatchResult:
    """Everything one detection run produced.

    ``findings`` is ranked by :func:`~core.mismatch.model.rank_findings` and
    numbered ``MM-0001`` upwards, so it is already the order the report, the
    CSV and the UI all want; :meth:`summary` is
    :func:`~core.mismatch.model.summarize` of exactly that list.

    The other fields are the diagnostic half of PLAN_MISMATCH §4's
    ``stages/mismatch.json``: the resolved policy and its hash, the units the
    run measured in, how many features each family contributed, what
    extraction threw away, how far each check actually looked, the fit context
    the tolerances were widened by, and the per-check timings.
    """

    findings: list = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)
    timings_s: dict = field(default_factory=dict)
    policy: object = None
    policy_hash: str = ""
    mm_per_unit: float = 1.0
    mech_insunits: int = 0
    mech_header_insunits: int = 0
    str_insunits: int = 0
    str_header_insunits: int = 0
    units_assumed: bool = False
    feature_counts: dict = field(default_factory=dict)
    fit_context: dict = field(default_factory=dict)
    inventory: dict = field(default_factory=dict)
    offset_rules: dict = field(default_factory=dict)
    offset_clusters: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    truncated: bool = False
    out_dir: object = None
    affine: object = None
    status: str = "ok"
    reason: str = ""
    family_coverage: dict = field(default_factory=dict)
    source_docs: str = "converted"

    # ── convenience ────────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self.findings)

    def __iter__(self):
        return iter(self.findings)

    def summary(self) -> dict:
        """``{total, by_severity, by_kind, by_origin}`` of the kept findings."""
        from .model import summarize
        return summarize(self.findings)

    def to_dict(self) -> dict:
        """A JSON-safe dump — the shape of ``stages/mismatch.json``."""
        from .model import finding_to_dict
        return {
            "status": self.status,
            "reason": self.reason,
            "source_docs": self.source_docs,
            "policy": self.policy.to_dict() if self.policy is not None else None,
            "policy_hash": self.policy_hash,
            "units": {"mech_insunits": self.mech_insunits,
                      "mech_header_insunits": self.mech_header_insunits,
                      "str_insunits": self.str_insunits,
                      "str_header_insunits": self.str_header_insunits,
                      "mm_per_unit": self.mm_per_unit,
                      "assumed": self.units_assumed},
            "features": self.feature_counts,
            "skipped": self.skipped,
            "coverage": self.coverage,
            "family_coverage": dict(self.family_coverage),
            "inventory": dict(self.inventory),
            "offset_rules": dict(self.offset_rules),
            "offset_clusters": list(self.offset_clusters),
            "summary": self.summary(),
            "fit_context": self.fit_context,
            "timings_s": {k: round(float(v), 6) for k, v in self.timings_s.items()},
            "warnings": list(self.warnings),
            "truncated": self.truncated,
            "findings": [finding_to_dict(f) for f in self.findings],
        }


# ── the detector ────────────────────────────────────────────────────────────

def detect(mech_doc, str_doc, affine=None, *, policy=None, fit_ctx=None,
           out_dir=None, prefix=None, axes=None, classifier=None,
           mech_layout=None, str_layout=None, bands: bool = True,
           max_findings=None, mech_unit=None, str_unit=None,
           force: bool = False, inventory: bool = False) -> MismatchResult:
    """Detect every mismatch between the MECH drawing and the STR drawing.

    Parameters
    ----------
    mech_doc, str_doc
        the two ezdxf documents, **or** paths to them.  The MECH file is read
        as the reference frame; the STR file is mapped into it with ``affine``.
    affine
        the STR → MECH transform (:class:`core.transform2d.Affine2D`, a
        ``dict`` from ``transform.json``, a 6-sequence, or ``None`` for the
        identity).  When ``None`` *and* ``prefix``/``axes`` are given, the
        transform is solved from the two grids with
        :func:`core.fit.solve`; with neither, the drawings are assumed already
        aligned, which is what the synthetic and unit callers want.
    policy
        a :class:`~core.mismatch.policy.MismatchPolicy`, or a **partial**
        policy ``dict`` merged over :data:`~core.mismatch.policy.DEFAULT_POLICY`
        (the same precedence rule as :func:`~core.mismatch.policy.load_policy`,
        minus the two file lookups).  ``None`` means the defaults.
    fit_ctx
        ``transform.json`` (a dict, a path, or ``None``).  It is what widens
        every tolerance (§3); without it the tolerances are exactly the ones in
        the policy.
    out_dir
        remembered on the result and otherwise unused — writing the artifacts
        of a run is 8b.5's job, not this function's.
    mech_unit, str_unit
        the units each drawing is known to be in (``"mm"``, ``"in"``, …).
        They win over the drawings' own ``$INSUNITS`` headers, which is how
        a user's explicit override reaches every millimetre threshold here.
    force
        the user confirmed the axes with ``force`` — they said not to trust
        the fit.  The comparison is then blocked even if every gate
        happened to pass, because "we could not tell" and "they agree" are
        different answers.
    inventory
        build the per-family / per-prefix inventory of what the drawing
        contained.  It costs a scan of the modelspace, so it is off by
        default.

    Returns
    -------
    MismatchResult
        ranked findings plus coverage, skipped counters and per-check timings.
    """
    import ezdxf

    from .checks import bolts as _bolts
    from .checks import clash as _clash
    from .checks import containment as _containment
    from .checks import offset as _offset
    from .checks import support as _support
    from .checks import Env
    from .classify import Classifier
    from .context import FitContext, analyse
    from .features import extract_pair, mm_per_unit_of
    from .model import rank_findings, truncate
    from .policy import DEFAULT_POLICY, MismatchPolicy

    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    warnings: list[str] = []

    pol = _resolve_policy(policy, DEFAULT_POLICY, MismatchPolicy)
    mech = _as_doc(mech_doc, ezdxf)
    str_ = _as_doc(str_doc, ezdxf)
    # A profile (profiles/<name>.json) can override the prefixes, the size
    # ranges and the families — but only for the drawing it describes.
    pol = _apply_profile(pol, str_)
    affine = _resolve_affine(affine, mech, str_, prefix, axes, warnings,
                              mech_unit=mech_unit, str_unit=str_unit)

    mm, assumed = mm_per_unit_of(mech, mech_unit)
    if assumed:
        warnings.append("units_assumed_mm: the MECH drawing has no $INSUNITS; "
                        "millimetres were assumed")

    clf = classifier if classifier is not None else Classifier(pol)

    t = time.perf_counter()
    mech_fs, str_fs = extract_pair(mech, str_, affine=affine, policy=pol, classifier=clf,
                                   mm_per_unit=mm, mech_layout=mech_layout,
                                   str_layout=str_layout, bands=bands)
    timings["extract"] = time.perf_counter() - t

    fit = fit_ctx if isinstance(fit_ctx, FitContext) else \
        FitContext.from_transform_json(fit_ctx, mm_per_unit=mm,
                                       k_fit=float(pol.context.k_fit))
    env = Env(policy=pol, mm=mm, mech=mech_fs, str=str_fs, clf=clf, fit=fit,
              mech_doc=mech, str_doc=str_,
              mech_root=None, str_root=_str_root(affine),
              # `FitContext.median_spacing_mm` is ALREADY in millimetres
              # (context.py converts transform.json's MECH-unit spacing).
              # Multiplying by `mm` again made the §1.4 search cap 1000×
              # too loose on a metre drawing.
              grid_spacing_mm=fit.median_spacing_mm)
    if fit.weak:
        warnings.append(f"fit_quality_{fit.status}: the grid fit did not pass its "
                        f"quality gate; every finding is marked low confidence")
    # How many rule legs the policy has — the denominator of the coverage
    # the status is decided on.
    env.total_pairs = (len(pol.pairs.bolts) + len(pol.pairs.support)
                       + len(pol.pairs.containment) + len(pol.pairs.clash))

    findings: list = []
    for name, module in (("bolts", _bolts), ("support", _support),
                         ("containment", _containment), ("clash", _clash),
                         ("offset", _offset)):
        t = time.perf_counter()
        findings += module.run(env)
        timings[name] = time.perf_counter() - t

    t = time.perf_counter()
    findings = analyse(env, findings, fit)
    timings["context"] = time.perf_counter() - t

    t = time.perf_counter()
    ranked = rank_findings(findings)
    limit = pol.max_findings if max_findings is None else max_findings
    ranked, truncated = truncate(ranked, limit)
    timings["rank"] = time.perf_counter() - t
    timings["total"] = time.perf_counter() - t0

    # ── the status ────────────────────────────────────────────────────
    # "ok" used to mean "no findings", which also meant "nothing was
    # compared" when the drawings had no recognisable structure.  The
    # status now says what the check actually did.
    from . import status as status_mod
    for counts in env.family_coverage.values():
        counts["unmatched"] = counts["eligible"] - counts["matched"]

    # The findings a provisional rule (or an ambiguity) produced are not
    # defects: they are questions for an engineer.
    review_rules = []
    n_review = 0
    for f in ranked:
        d = getattr(f, "details", None) or {}
        if d.get("ambiguous") or d.get("needs_review"):
            n_review += 1
            rule = str(d.get("rule") or "")
            if rule and rule not in review_rules:
                review_rules.append(rule)

    m_status, m_reason = status_mod.decide(
        covered_pairs=env.covered_pairs,
        total_pairs=env.total_pairs,
        n_findings=len(ranked),
        fit_trusted=(not fit.weak) and not force,
        fit_status=("forced" if force and not fit.weak else fit.status),
        n_review=n_review,
        review_rules=tuple(review_rules),
    )

    clusters: list = []
    try:
        from .checks.offset import cluster as _cluster
        clusters = _cluster(ranked)
    except Exception:
        clusters = []

    inv = {}
    if inventory:
        inv = {"MECH": build_inventory(mech, mech_fs, clf, "MECH"),
               "STR": build_inventory(str_, str_fs, clf, "STR")}

    return MismatchResult(
        findings=ranked, coverage=env.coverage,
        family_coverage=dict(env.family_coverage),
        inventory=inv,
        offset_rules=dict(env.rule_notes),
        offset_clusters=clusters,
        status=m_status, reason=m_reason,
        skipped={"MECH": dict(mech_fs.skipped), "STR": dict(str_fs.skipped)},
        timings_s=timings, policy=pol, policy_hash=pol.policy_hash,
        mm_per_unit=mm,
        # The *chosen* code, not the header's: with an override the two
        # differ, and the report must say which one was used.
        mech_insunits=_chosen_code(mech_unit, mech),
        mech_header_insunits=int(mech.header.get("$INSUNITS", 0) or 0),
        str_insunits=_chosen_code(str_unit, str_),
        str_header_insunits=int(str_.header.get("$INSUNITS", 0) or 0),
        units_assumed=bool(assumed),
        feature_counts={"MECH": mech_fs.counts(), "STR": str_fs.counts()},
        fit_context=fit.to_dict(), warnings=warnings, truncated=bool(truncated),
        out_dir=out_dir, affine=affine)


def _str_root(affine):
    """The matrix the STR drawing is read through — into MECH world."""
    try:
        return affine.to_matrix44() if affine is not None else None
    except Exception:
        return None


def _apply_profile(pol, doc):
    """Apply the profile in force, if this drawing is the one it describes."""
    from .policy import apply_profile
    try:
        return apply_profile(pol, doc)
    except Exception as exc:              # noqa: BLE001 — a bad profile must not
        print(f"  profile not applied: {exc}")   # kill the whole check
        return pol


def _prefix_of(name: str) -> str:
    """The stable part of a block name: ``Part-116860372-114953`` → ``Part-``."""
    name = str(name or "")
    return name.split("-")[0] + "-" if "-" in name else name


def build_inventory(doc, fs, clf, source: str) -> dict:
    """What the drawing contained, and what became geometry.

    The counts are per **family** (what the checks can use), per
    **prefix** (what the CAD export put in the drawing) and per
    **layer/block combination** (where it came from) — plus the
    combinations that produced nothing, so an unrecognised drawing is
    visible rather than silently empty.
    """
    from collections import Counter

    # the prefixes whose blocks are empty — there is nothing to measure
    empty_prefixes = set()
    try:
        for b in doc.blocks:
            if len(b) == 0:
                empty_prefixes.add(_prefix_of(b.name))
    except Exception:
        pass

    inserts = Counter()
    per_prefix = Counter()
    for e in doc.modelspace():
        if e.dxftype() != "INSERT":
            continue
        name = str(e.dxf.name or "")
        prefix = _prefix_of(name)
        try:
            layer = str(e.dxf.layer or "0")
        except Exception:
            layer = "0"
        inserts[(layer, prefix)] += 1
        per_prefix[prefix] += 1

    families = Counter()
    recognised = set()
    for f in fs.features:
        for fam in f.families:
            families[fam] += 1
        if f.insert_chain:
            recognised.add(_prefix_of(f.insert_chain[-1]))

    # The blocks each family claims — the prefix rule decides, so a
    # family can be inventoried even when the drawing's blocks are empty.
    by_prefix_family = Counter()
    for f in fs.features:
        if f.insert_chain:
            prefix = _prefix_of(f.insert_chain[-1])
            for fam in f.families:
                by_prefix_family[(prefix, fam)] += 1
    blocks_by_family = Counter()
    for (layer, prefix), n in inserts.items():
        rule = clf.prefix_rule([prefix]) or {}
        if rule.get("family"):
            blocks_by_family[rule["family"]] += n
            continue
        fams = {fam for (p, fam) in by_prefix_family if p == prefix}
        if len(fams) == 1:
            blocks_by_family[fams.pop()] += n
        elif fams:
            for (p, fam), k in by_prefix_family.items():
                if p == prefix:
                    blocks_by_family[fam] += k

    unrecognised = []
    for (layer, prefix), n in sorted(inserts.items()):
        rule = clf.prefix_rule([prefix]) or {}
        kind = rule.get("kind")
        if prefix in recognised:
            continue
        if kind in ("annotation", "grid", "reference"):
            why = f"{kind} — never geometry"
        elif rule.get("family"):
            why = ("the blocks are empty — there is nothing to measure"
                   if prefix in empty_prefixes else
                   "the block's shape matched no rule")
        else:
            why = "no family recognises this block"
        unrecognised.append({"layer": layer, "prefix": prefix,
                             "inserts": n, "why": why})

    return {
        "source": source,
        "inserts": int(sum(per_prefix.values())),
        "prefixes": dict(sorted(per_prefix.items())),
        "families": dict(sorted(families.items())),
        "blocks_by_family": dict(sorted(blocks_by_family.items())),
        "layers": {f"{layer}|{prefix}": n
                   for (layer, prefix), n in sorted(inserts.items())},
        "unrecognised": unrecognised,
    }


def _chosen_code(unit, doc) -> int:
    """The ``$INSUNITS`` code actually in force: the override, else the header."""
    from .. import units as units_mod
    name = units_mod.normalise_unit(unit)
    if name:
        return units_mod.UNIT_CODES[name]
    try:
        return int(doc.header.get("$INSUNITS", 0) or 0)
    except Exception:
        return 0


# ── the stage entry point ───────────────────────────────────────────────────

def run(job_id: str, *, pair_dir: Path | None = None) -> None:
    """Stage 8b.5: detect the MECH⇄STR mismatches for one pair.

    Writes ``<root>/stages/mismatch.json`` and draws the QA markers into
    ``<root>/out/superimposed.dxf``.  **Non-fatal**: on any error it writes
    ``<root>/out/findings.json`` with ``status="failed"`` and a message, and
    returns normally — the UI must never show "0 findings" for a check that
    did not run.

    The status is the check's own vocabulary (``passed`` /
    ``discrepancies_found`` / ``insufficient_coverage`` / ``blocked`` /
    ``failed`` / ``disabled``) — see :mod:`core.mismatch.status`.  It is
    deliberately **not** the job's or the overlay's status.
    """
    import json

    from jobs import job_dir

    from core import dxfio
    from core.roles import resolve_pair

    from .policy import load_policy

    root = Path(pair_dir).parent if pair_dir is not None else job_dir(job_id)
    stages = root / "stages"
    out_dir = root / "out"
    stages.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    mismatch_path = stages / "mismatch.json"
    findings_path = out_dir / "findings.json"

    def _write_error(message: str) -> None:
        payload = {"status": "failed", "message": message,
                   "reason": f"the mismatch check raised: {message}",
                   "findings": [], "warnings": [message]}
        try:
            findings_path.write_text(json.dumps(payload, indent=2))
            mismatch_path.write_text(json.dumps(payload, indent=2))
        except Exception:
            pass

    try:
        pol = load_policy(job_id, pair_root=root)

        # `policy.enabled` — a disabled check is not "zero findings".
        if not pol.enabled:
            payload = {"status": "disabled", "source_docs": pol.source_docs,
                       "policy": pol.to_dict(), "policy_hash": pol.policy_hash,
                       "reason": "mismatch detection is disabled by policy",
                       "coverage": {}, "family_coverage": {},
                       "warnings": ["mismatch detection is disabled by policy"],
                       "findings": []}
            mismatch_path.write_text(json.dumps(payload, indent=2))
            findings_path.write_text(json.dumps(
                {"status": "disabled", "generated_at": _now_iso(),
                 "units": {}, "summary": {}, "coverage": {},
                 "family_coverage": {},
                 "reason": "mismatch detection is disabled by policy",
                 "warnings": ["mismatch detection is disabled by policy"],
                 "truncated": False, "findings": []}, indent=2))
            return

        # ── the source drawings ───────────────────────────────────────────
        warnings: list[str] = []
        source_docs = pol.source_docs
        try:
            mech_path, str_path = resolve_pair(job_id, pair_dir,
                                               stage=source_docs)
        except Exception:
            if source_docs != "cleaned":
                source_docs = "cleaned"
                mech_path, str_path = resolve_pair(job_id, pair_dir,
                                                   stage="cleaned")
                warnings.append(
                    "source_docs_fallback_cleaned: the converted MECH/STR DXF "
                    "was not found; the cleaned files were used instead")
            else:
                raise

        # ── the fit ───────────────────────────────────────────────────────
        transform_path = root / "transform.json"
        transform = json.loads(transform_path.read_text())

        # The units of each drawing: the user's override when there is one.
        # They decide what every millimetre threshold in this stage means.
        mech_unit = str_unit = None
        try:
            from core.roles import units_for_pair
            mech_u, str_u = units_for_pair(job_id)
            # Only an *override* is passed down: with no override the
            # document's own header decides, and a unitless drawing still
            # reports "assumed mm" the way it always has.
            mech_unit = mech_u.override
            str_unit = str_u.override
        except Exception as exc:              # noqa: BLE001 — advisory only
            warnings.append(f"units_unavailable: {exc}")

        # The user confirmed the axes with force: they said not to trust
        # the fit, and the comparison must honour that.
        try:
            from core.fit import options_for_job
            forced = bool(options_for_job(job_id).force)
        except Exception:
            forced = False

        result = detect(mech_path, str_path, affine=transform, policy=pol,
                        fit_ctx=transform, out_dir=out_dir,
                        mech_unit=mech_unit, str_unit=str_unit,
                        force=forced, inventory=True)
        result.source_docs = source_docs
        for w in warnings:
            result.warnings.append(w)

        payload = result.to_dict()
        mismatch_path.write_text(json.dumps(payload, indent=2, default=str))

        # ── the QA markers in the superimposed DXF ─────────────────────────
        try:
            from .render import draw_into
            draw_into(root, result, policy=pol)
        except Exception as exc:            # noqa: BLE001 — non-fatal
            result.warnings.append(f"mismatch markers not drawn: {exc}")

    except Exception as exc:                # noqa: BLE001 — non-fatal by contract
        _write_error(f"{type(exc).__name__}: {exc}")
        print(f"  mismatch check failed (non-fatal): {exc}")
        return


def _now_iso() -> str:
    """The current UTC time as an ISO-8601 string (second resolution)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ── input plumbing ──────────────────────────────────────────────────────────

def _as_doc(doc, ezdxf):
    if hasattr(doc, "modelspace"):
        return doc
    # PLAN.md step 2: every read goes through core.dxfio — `ezdxf.readfile`
    # must not appear outside core/dxfio.py and core/package.py.
    from core.dxfio import read_internal
    return read_internal(str(doc))


def _resolve_policy(policy, DEFAULT_POLICY, MismatchPolicy) -> MismatchPolicy:
    """Accept a policy object or a partial dict; merge the dict over the defaults.

    Merging is one **top-level section** at a time, so ``{"checks": {…}}`` keeps
    the default families and tolerances — the same rule
    :func:`~core.mismatch.policy.load_policy` applies to the files.
    """
    if policy is None:
        return DEFAULT_POLICY
    if isinstance(policy, MismatchPolicy):
        return policy
    if isinstance(policy, dict):
        base = dict(DEFAULT_POLICY.to_dict())
        base.update(policy)
        return MismatchPolicy.from_dict(base)
    raise TypeError(f"policy must be a MismatchPolicy, a dict or None, "
                    f"got {type(policy).__name__}")


def _resolve_affine(affine, mech, str_, prefix, axes, warnings, *,
                    mech_unit=None, str_unit=None):
    """The STR → MECH transform, solved from the grids when none was given.

    Solving needs the label convention, which in this project is a *human*
    confirmation (step 3 writes ``axes/convention.json`` and waits), so it is
    only ever attempted when the caller passes ``prefix`` and ``axes``; the
    gates are downgraded to warnings because a mismatch run must not die on a
    marginal fit — it reports the fit's quality instead.
    """
    from core.transform2d import Affine2D

    if affine is not None:
        return _as_affine(affine)
    if not (prefix and axes):
        return Affine2D()
    import numpy as np

    from core import fit as _fit
    from core import grid as _grid
    from core import units as units_mod

    mg = _grid.detect_grid(mech, prefix, list(axes))
    sg = _grid.detect_grid(str_, prefix, list(axes))
    mi, si = _grid.intersections(mg), _grid.intersections(sg)
    keys = sorted(set(mi) & set(si))
    if len(keys) < 2:
        warnings.append("grid fit skipped: fewer than two shared grid "
                        "intersections; the drawings are assumed to be aligned")
        return Affine2D()
    sol = _fit.solve(_fit.FitInputs(
        labels=[f"{a}-{b}" for a, b in keys],
        src=np.array([[si[k].x, si[k].y] for k in keys], dtype=float),
        dst=np.array([[mi[k].x, mi[k].y] for k in keys], dtype=float),
        spacing=_fit.median_spacing(mg), extent=float(mg.extent) or 0.0,
        str_units=_chosen_code(str_unit, str_),
        mech_units=_chosen_code(mech_unit, mech),
        str_unit_name=units_mod.normalise_unit(str_unit) or "",
        mech_unit_name=units_mod.normalise_unit(mech_unit) or ""),
        options=_fit.FitOptions(force=True))
    return sol["affine"]


def _as_affine(x):
    """``Affine2D`` from an Affine2D, a dict, a 6-sequence or a Matrix44."""
    from ezdxf.math import Matrix44

    from core.transform2d import Affine2D

    if isinstance(x, Affine2D):
        return x
    if isinstance(x, Matrix44):
        return Affine2D.from_matrix44(x)
    if isinstance(x, dict):
        return Affine2D.from_json(x)
    seq = list(x)
    if len(seq) == 6:
        return Affine2D(*[float(v) for v in seq])
    raise TypeError(f"cannot read an affine transform from {type(x).__name__}")