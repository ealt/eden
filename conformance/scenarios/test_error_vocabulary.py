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

# Chapter 7 §7 closed v0 vocabulary.
_V0_VOCABULARY: frozenset[str] = frozenset(
    {
        "eden://error/bad-request",
        "eden://error/experiment-id-mismatch",
        "eden://error/wrong-token",
        "eden://error/not-found",
        "eden://error/already-exists",
        "eden://error/illegal-transition",
        "eden://error/conflicting-resubmission",
        "eden://error/invalid-precondition",
    }
)


@pytest.mark.run(order=-2)
def test_observed_types_are_in_v0_vocabulary(
    session_observed_problem_types: set[str],
) -> None:
    """spec/v0/07-wire-protocol.md §7 — every observed `type` is in the §7 closed table."""
    extras = session_observed_problem_types - _V0_VOCABULARY
    assert not extras, f"observed `type` URIs outside §7 vocabulary: {sorted(extras)}"


@pytest.mark.run(order=-1)
def test_v0_vocabulary_each_observed_at_least_once(
    session_observed_problem_types: set[str],
) -> None:
    """spec/v0/07-wire-protocol.md §7 — every §7 entry is exercised by some scenario."""
    missing = _V0_VOCABULARY - session_observed_problem_types
    assert not missing, (
        f"§7 vocabulary entries never observed during the run: {sorted(missing)}"
    )
