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

# Chapter 7 §7 closed v0 vocabulary (post-12a-1, post-#148). Split
# into three tiers:
#
#   * `_CORE_VOCABULARY` — types every IUT MUST emit at some point
#     during the suite; `test_v0_vocabulary_each_observed_at_least_once`
#     asserts that. Includes ``unauthorized`` and ``forbidden`` since
#     the reference adapter runs auth-enabled post-#148 (the prior
#     `X-Eden-Worker-Id` test-fixture header is retired and the §13
#     bearer scheme is the only auth path).
#   * `_AUTH_DISABLED_OBSERVABLE_TYPES` — wire types that are
#     only observable through an auth-disabled IUT. Under the §13
#     auth-enabled posture, an unregistered worker_id has no valid
#     bearer to present, so the wire's auth middleware 401s before
#     the chapter 04 §3.5 step-2 ``worker-not-registered`` check ever
#     fires. The Store-level MUST stays; it's a Store-API contract
#     observable to in-process callers but not to a wire-only IUT
#     running under auth.
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
_AUTH_DISABLED_OBSERVABLE_TYPES: frozenset[str] = frozenset(
    {
        # Chapter 04 §3.5 step 2: under §13 auth-enabled IUTs, an
        # unregistered worker_id cannot present a valid bearer, so the
        # wire returns 401 ``unauthorized`` before reaching the
        # registration check. Only auth-disabled IUTs surface this
        # through the wire.
        "eden://error/worker-not-registered",
    }
)

_IMPL_OPTIONAL_TYPES: frozenset[str] = frozenset(
    {
        # spec/v0/03-roles.md §3.4: rejection MAY surface at submit
        # (4xx with this type), at accept (no wire envelope; routed
        # via validation-error), or both. End-state guarantee is on
        # the variant, not the wire surface.
        "eden://error/no-op-variant",
        # spec/v0/07-wire-protocol.md §9 + §14, spec/v0/10-checkpoints.md:
        # the v1+checkpoints conformance level (chapter 9 §4) adds these
        # four wire types. They are MANDATORY for impls that claim that
        # level (the import endpoint emits them on the documented
        # failure paths), but an impl claiming only v1 / v1+roles /
        # v1+roles+integrator MAY omit the checkpoint endpoints entirely
        # — in which case these types never surface. Treat as
        # optional-at-observation to match.
        "eden://error/checkpoint-invalid",
        "eden://error/experiment-id-conflict",
        "eden://error/spec-version-mismatch",
        "eden://error/unsupported-checkpoint-version",
        # spec/v0/07-wire-protocol.md §9 + spec/v0/11-control-plane.md
        # §4.5: the v1+multi-experiment conformance level (chapter 9
        # §4) adds these four wire types. MANDATORY for impls that
        # claim that level (the lease ops emit them on the documented
        # failure paths), but an impl claiming only v1 / v1+roles /
        # v1+roles+integrator / v1+checkpoints MAY omit the chapter-11
        # surface entirely — in which case these types never surface.
        # Same impl-optional posture as the checkpoint types above.
        "eden://error/lease-held-by-other",
        "eden://error/lease-not-held",
        "eden://error/lease-expired",
        "eden://error/lease-instance-mismatch",
    }
)

_CORE_VOCABULARY: frozenset[str] = frozenset(
    {
        "eden://error/bad-request",
        "eden://error/experiment-id-mismatch",
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
        "eden://error/unauthorized",
        "eden://error/forbidden",
    }
)

_V0_VOCABULARY: frozenset[str] = (
    _CORE_VOCABULARY | _AUTH_DISABLED_OBSERVABLE_TYPES | _IMPL_OPTIONAL_TYPES
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

    The auth-disabled-observable types (``worker-not-registered``)
    and the impl-optional types (``no-op-variant``: spec §3.4
    latitude on rejection point; the v1+checkpoints /
    v1+multi-experiment levels) are scoped out of this MUST-observe
    assertion: the reference adapter runs auth-enabled, so the
    ``worker-not-registered`` chapter 04 §3.5 step-2 check is
    shadowed by the auth middleware's 401; impl-optional because
    the spec explicitly permits rejecting silently / the type only
    surfaces in a conformance level the IUT may not claim. The
    first direction — every observed type is in the closed
    vocabulary — already covers them: an adapter that emits one
    outside the table fails ``test_observed_types_are_in_v0_vocabulary``.
    """
    missing = _CORE_VOCABULARY - session_observed_problem_types
    assert not missing, (
        f"§7 vocabulary entries never observed during the run: {sorted(missing)}"
    )
