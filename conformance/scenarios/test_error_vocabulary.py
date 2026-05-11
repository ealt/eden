"""Error vocabulary closure — chapter 07 §7.

The closed v0 vocabulary is asserted in two directions:

1. Every observed `eden://error/...` `type` URI belongs to the closed table.
2. Every entry of the closed table is observed by at least one earlier scenario.

Both assertions live in tests that pytest-ordering schedules to run AFTER all
other conformance scenarios have populated the session-scoped observation set.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Error vocabulary closure'

# Chapter 7 §7 closed v0 vocabulary (post-12a-1). Split into two
# tiers because the reference conformance adapter runs the IUT with
# auth disabled (see `adapters/reference/adapter.py` rationale).
# Auth-only error types (``unauthorized`` / ``forbidden``) can only
# be elicited against an auth-enabled IUT; the harness asserts they
# fall within the closed vocabulary when observed but does not
# require observation under auth-disabled fixtures.
_AUTH_ONLY_TYPES: frozenset[str] = frozenset(
    {
        "eden://error/unauthorized",
        "eden://error/forbidden",
    }
)

_CORE_VOCABULARY: frozenset[str] = frozenset(
    {
        "eden://error/bad-request",
        "eden://error/experiment-id-mismatch",
        "eden://error/worker-not-registered",
        "eden://error/worker-not-eligible",
        "eden://error/wrong-claimant",
        "eden://error/not-found",
        "eden://error/already-exists",
        "eden://error/illegal-transition",
        "eden://error/not-claimed",
        "eden://error/conflicting-resubmission",
        "eden://error/invalid-precondition",
        "eden://error/reserved-identifier",
        "eden://error/cycle-detected",
    }
)

_V0_VOCABULARY: frozenset[str] = _CORE_VOCABULARY | _AUTH_ONLY_TYPES


@pytest.mark.run(order=-2)
def test_observed_types_are_in_v0_vocabulary(
    session_observed_problem_types: set[str],
) -> None:
    """spec/v0/07-wire-protocol.md §9 — every observed `type` is in the §7 closed table."""
    extras = session_observed_problem_types - _V0_VOCABULARY
    assert not extras, f"observed `type` URIs outside §7 vocabulary: {sorted(extras)}"


@pytest.mark.run(order=-1)
def test_v0_vocabulary_each_observed_at_least_once(
    session_observed_problem_types: set[str],
) -> None:
    """spec/v0/07-wire-protocol.md §9 — every §7 entry is exercised by some scenario.

    The auth-only types (``unauthorized`` / ``forbidden``) are scoped
    out of this MUST-observe assertion: they can only be elicited
    against an auth-enabled IUT, and the reference adapter runs with
    auth disabled (worker_id forwarded via ``X-Eden-Worker-Id`` per
    chapter 07 §13's binding-defined posture). The first direction —
    every observed type is in the closed vocabulary — already covers
    them: an auth-enabled adapter that emits one outside the table
    fails ``test_observed_types_are_in_v0_vocabulary``.
    """
    missing = _CORE_VOCABULARY - session_observed_problem_types
    assert not missing, (
        f"§7 vocabulary entries never observed during the run: {sorted(missing)}"
    )
