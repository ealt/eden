"""Chapter 07 §7 closed error vocabulary — shared by the scenario and the plugin.

The vocabulary-closure assertions in
:mod:`conformance.scenarios.test_error_vocabulary` are *suite-level*
checks over every ``eden://error/...`` ``type`` the IUT emits during a
run (accumulated in the session-scoped ``observed_problem_types`` set).
Under ``pytest-xdist`` the accumulator is per-worker, so the
closure cannot be asserted inside a single worker's session — it is
aggregated across workers in :mod:`conformance.harness.plugin` at
controller session-finish.

Both consumers (the serial-path scenario tests and the xdist-path
controller hook) need the same closed-vocabulary tables and the same
closure logic, so they live here in the harness layer rather than in
the scenario module. The tables are spec-derived constants (chapter 07
§7); see ``test_error_vocabulary.py`` for the prose rationale on each
tier.
"""

from __future__ import annotations

from collections.abc import Iterable

# Wire types that are only observable through an auth-DISABLED IUT.
# Under the §13 auth-enabled posture the wire 401s an unregistered
# worker before the chapter 04 §3.5 step-2 ``worker-not-registered``
# check can fire, so the reference adapter (auth-enabled) never emits
# this on the wire. In-vocabulary, but not required-to-observe.
AUTH_DISABLED_OBSERVABLE_TYPES: frozenset[str] = frozenset(
    {
        "eden://error/worker-not-registered",
    }
)

# Closed-vocab types whose wire surface is impl-defined per spec
# latitude, or that only surface in a conformance level the IUT MAY
# decline (v1+checkpoints, v1+multi-experiment). In-vocabulary, but
# not required-to-observe in any given session.
IUT_OPTIONAL_TYPES: frozenset[str] = frozenset(
    {
        # spec/v0/03-roles.md §3.4 — no-op rejection MAY surface at
        # submit, at accept (no wire envelope), or both.
        "eden://error/no-op-variant",
        # spec/v0/07-wire-protocol.md §9 + §14 + spec/v0/10-checkpoints.md —
        # v1+checkpoints level; omitted entirely by impls that do not
        # claim it.
        "eden://error/checkpoint-invalid",
        "eden://error/experiment-id-conflict",
        "eden://error/spec-version-mismatch",
        "eden://error/unsupported-checkpoint-version",
        # spec/v0/07-wire-protocol.md §9 + spec/v0/11-control-plane.md §4.5 —
        # v1+multi-experiment level; omitted entirely by impls that do
        # not claim it.
        "eden://error/lease-held-by-other",
        "eden://error/lease-not-held",
        "eden://error/lease-expired",
        "eden://error/lease-instance-mismatch",
    }
)

# Types every IUT MUST emit at some point during the suite.
CORE_VOCABULARY: frozenset[str] = frozenset(
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

# The full closed v0 vocabulary (chapter 07 §7).
V0_VOCABULARY: frozenset[str] = (
    CORE_VOCABULARY | AUTH_DISABLED_OBSERVABLE_TYPES | IUT_OPTIONAL_TYPES
)


def out_of_vocabulary(observed: Iterable[str]) -> set[str]:
    """Observed ``type`` URIs that fall OUTSIDE the §7 closed table.

    Non-empty means the IUT emitted a ``type`` not in the closed
    vocabulary — a chapter 07 §9 violation.
    """
    return set(observed) - V0_VOCABULARY


def unobserved_core(observed: Iterable[str]) -> set[str]:
    """Core §7 entries the run never exercised.

    Non-empty means the *suite* failed to drive some required error
    type — a coverage gap, asserted once over the whole run.
    """
    return set(CORE_VOCABULARY) - set(observed)
