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

# Chapter 7 §7 closed v0 vocabulary (post-12a-1). Split into three
# tiers:
#
#   * `_CORE_VOCABULARY` — types every IUT MUST emit at some point
#     during the suite; `test_v0_vocabulary_each_observed_at_least_once`
#     asserts that.
#   * `_AUTH_ONLY_TYPES` — ``unauthorized`` / ``forbidden``: only
#     elicited against an auth-enabled IUT; the reference adapter
#     runs with auth disabled (see `adapters/reference/adapter.py`).
#   * `_IMPL_OPTIONAL_TYPES` — closed-vocab types whose surface on
#     the wire is impl-defined per spec latitude. ``no-op-variant``
#     is the canonical example: spec/v0/03-roles.md §3.4 allows the
#     rejection to surface at submit OR accept OR not at all on the
#     wire (the spec's MUST is on the end-state, not on the wire
#     envelope). A conforming IUT that rejects no-ops only at accept
#     time via §4.3's validation-error path never emits the type.
#
# All three tiers belong to the closed v0 vocabulary
# (``test_observed_types_are_in_v0_vocabulary``), but only `_CORE_VOCABULARY`
# is required to be observed in any given session.
_AUTH_ONLY_TYPES: frozenset[str] = frozenset(
    {
        "eden://error/unauthorized",
        "eden://error/forbidden",
    }
)

_IMPL_OPTIONAL_TYPES: frozenset[str] = frozenset(
    {
        # spec/v0/03-roles.md §3.4: rejection MAY surface at submit
        # (4xx with this type), at accept (no wire envelope; routed
        # via validation-error), or both. End-state guarantee is on
        # the variant, not the wire surface.
        "eden://error/no-op-variant",
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

_V0_VOCABULARY: frozenset[str] = (
    _CORE_VOCABULARY | _AUTH_ONLY_TYPES | _IMPL_OPTIONAL_TYPES
)


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
    """spec/v0/07-wire-protocol.md §9 — every §7 core entry is exercised by some scenario.

    The auth-only types (``unauthorized`` / ``forbidden``) and the
    impl-optional types (``no-op-variant``: spec §3.4 latitude on
    rejection point) are scoped out of this MUST-observe assertion:
    auth-only because the reference adapter runs auth-disabled, and
    impl-optional because the spec explicitly permits rejecting
    silently (variant simply does not terminalize as success). The
    first direction —
    every observed type is in the closed vocabulary — already covers
    them: an auth-enabled adapter that emits one outside the table
    fails ``test_observed_types_are_in_v0_vocabulary``.
    """
    missing = _CORE_VOCABULARY - session_observed_problem_types
    assert not missing, (
        f"§7 vocabulary entries never observed during the run: {sorted(missing)}"
    )
