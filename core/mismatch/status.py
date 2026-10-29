#!/usr/bin/env python3
"""core/mismatch/status.py — the outcome vocabulary of the mismatch check.

The check used to report one of three things: ``ok``, ``disabled`` or
``error``.  ``ok`` was a lie in the common case: a drawing with no
recognisable structure produced *no findings* and was reported as ``ok``,
which reads as "the two drawings agree" when in fact **nothing was
compared**.  A reviewer who saw "ok" had no way to tell the two apart.

So the check now says what it actually did:

``passed``
    every rule that had items on both sides was compared, and they agreed.
``discrepancies_found``
    the same, but the comparison found something to report.
``insufficient_coverage``
    **no rule had items on both sides** — the drawings were not really
    compared, so nothing can be concluded.  This is never ``passed``.
``blocked``
    the grid fit was forced or is otherwise untrusted, so the alignment
    the comparison rests on cannot be relied on.
``failed``
    the check itself raised.
``disabled``
    the policy turned the check off.

This vocabulary is the **mismatch check's own**; it is deliberately not the
job's or the overlay's status (those stay ``done`` / ``failed``).  Both
``core/mismatch`` and ``core/findings`` import it from here, so the two
can never disagree about what a status means.
"""
from __future__ import annotations

#: The six outcomes.
PASSED = "passed"
DISCREPANCIES_FOUND = "discrepancies_found"
REVIEW_REQUIRED = "review_required"
INSUFFICIENT_COVERAGE = "insufficient_coverage"
BLOCKED = "blocked"
FAILED = "failed"
DISABLED = "disabled"

#: Every status, in the order of "how much was actually concluded".
STATUSES = (
    PASSED,
    DISCREPANCIES_FOUND,
    REVIEW_REQUIRED,
    INSUFFICIENT_COVERAGE,
    BLOCKED,
    FAILED,
    DISABLED,
)

#: The statuses that mean "the check did not conclude anything".
NOT_CONCLUDED = (INSUFFICIENT_COVERAGE, BLOCKED, FAILED, DISABLED)


def is_ok(status: str) -> bool:
    """Did the check actually compare the two drawings and agree?"""
    return status == PASSED


def decide(*, covered_pairs: int = 0, total_pairs: int = 0,
           n_findings: int = 0, fit_trusted: bool = True,
           fit_status: str = "", n_review: int = 0,
           review_rules: tuple = ()) -> tuple[str, str]:
    """The status and its one-line reason.

    ``covered_pairs`` — how many rule legs had items on **both** sides,
    i.e. were really compared.  ``total_pairs`` — how many rule legs the
    policy has.  ``fit_trusted`` — whether the grid fit behind the
    comparison can be relied on.  ``n_review`` — how many of the findings
    come from a provisional rule (or are ambiguous); ``review_rules`` —
    their names.
    """
    covered = int(covered_pairs or 0)
    total = int(total_pairs or 0)
    n = int(n_findings or 0)

    if not fit_trusted:
        why = (f"the grid fit was {fit_status or 'forced'}"
               if fit_status else "the grid fit is not trusted")
        return BLOCKED, (f"{why} — the alignment the comparison rests on "
                         "cannot be relied on")

    if covered <= 0:
        return INSUFFICIENT_COVERAGE, (
            f"no rule had items on both sides ({total} rule leg(s) checked, "
            "none comparable) — the two drawings were not really compared")

    if n > 0:
        if n_review >= n:
            # everything that was found needs a human, not a repair
            rules = ", ".join(str(r) for r in review_rules) or "a provisional rule"
            return REVIEW_REQUIRED, (
                f"{n} finding{'s' if n != 1 else ''} from provisional rule "
                f"{rules}, engineer review needed")
        return DISCREPANCIES_FOUND, (
            f"{covered} of {total} rule leg(s) compared items on both sides "
            f"and found {n} discrepanc{'y' if n == 1 else 'ies'}")

    return PASSED, (
        f"{covered} of {total} rule leg(s) compared items on both sides "
        "and found nothing to report")


def is_conclusive(status: str) -> bool:
    """Did the check compare the two drawings and reach a conclusion?

    ``passed`` and ``discrepancies_found`` are conclusions; the other four
    are not — they say the check could not conclude anything.
    """
    return status in (PASSED, DISCREPANCIES_FOUND)
