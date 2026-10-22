"""core/mismatch — MECH ⇄ STR mismatch detection (PLAN_MISMATCH step 8b).

Package marker for the detection stage.  The modules that exist today:

``model``
    :class:`~core.mismatch.model.Finding`, :class:`~core.mismatch.model.EntityRef`,
    the kind/severity vocabularies, deterministic ranking and the JSON/CSV
    serialisers.
``policy``
    :class:`~core.mismatch.policy.MismatchPolicy`, ``DEFAULT_POLICY`` and
    :func:`~core.mismatch.policy.load_policy`.

Still to come (later sub-steps of 8b, not built yet):

``features`` / ``classify``
    per-block feature extraction and family assignment.
``checks`` (bolts, support, containment, clash) and ``context``
    the checks themselves and the local-vs-systematic analysis.
``detect(mech_doc, str_doc, affine, *, policy, fit_ctx=None) -> MismatchResult``
``run(job_id, *, pair_dir=None)``
    the stage entry point.  Once ``run`` exists here,
    ``pipelines._run_stage_module("mismatch", job_id)`` finds it, because
    the loader imports ``core.mismatch`` and calls ``run``.

Importing this package must stay cheap and side-effect free; submodules
are imported explicitly (``from core.mismatch import policy``).
"""

__all__ = ["model", "policy"]