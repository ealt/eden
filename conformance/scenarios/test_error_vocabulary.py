"""Error vocabulary closure — chapter 07 §7.

The closed v0 vocabulary is asserted in two directions:

1. Every observed `eden://error/...` `type` URI belongs to the closed table.
2. Every entry of the closed table is observed by at least one earlier scenario.

Both assertions are *suite-level*: they read the session-scoped
``observed_problem_types`` accumulator that every other scenario
populates through its ``WireClient``. The closed-vocabulary tables and
the closure logic live in :mod:`conformance.harness.error_vocabulary`
so they can be shared with the harness plugin.

Two execution modes:

* **Serial** (no ``pytest-xdist``): the accumulator holds every type
  observed across the whole run, so the assertions run here as ordinary
  tests. ``pytest-ordering`` schedules them AFTER all other scenarios.
* **Distributed** (``pytest -n>0``): the accumulator is per-worker, so
  no single worker sees the whole run. These tests would assert against
  a partial set, so they ``skip`` on the worker; the closure is instead
  aggregated across workers and asserted at controller session-finish in
  :mod:`conformance.harness.plugin`. The skip preserves the
  chapter-9 §5 group's citation coverage (``check_citations.py`` reads
  the docstrings statically).
"""

from __future__ import annotations

import pytest
from conformance.harness.error_vocabulary import (
    out_of_vocabulary,
    unobserved_core,
)

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Error vocabulary closure'

# The closed-vocabulary tiers (core / auth-disabled-observable /
# impl-optional) and their rationale now live in
# ``conformance.harness.error_vocabulary``.

_XDIST_SKIP_REASON = (
    "vocabulary closure is aggregated across workers at controller "
    "session-finish under pytest-xdist (see conformance.harness.plugin)"
)


def _running_distributed(config: pytest.Config) -> bool:
    """True when this process is a pytest-xdist worker.

    ``workerinput`` is injected by xdist only on worker processes; its
    presence is the canonical idiom for "am I a distributed worker".
    """
    return hasattr(config, "workerinput")


@pytest.mark.run(order=-2)
def test_observed_types_are_in_v0_vocabulary(
    request: pytest.FixtureRequest,
    session_observed_problem_types: set[str],
) -> None:
    """spec/v0/07-wire-protocol.md §9 — every observed `type` is in the §7 closed table."""
    if _running_distributed(request.config):
        pytest.skip(_XDIST_SKIP_REASON)
    extras = out_of_vocabulary(session_observed_problem_types)
    assert not extras, f"observed `type` URIs outside §7 vocabulary: {sorted(extras)}"


@pytest.mark.run(order=-1)
def test_v0_vocabulary_each_observed_at_least_once(
    request: pytest.FixtureRequest,
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
    if _running_distributed(request.config):
        pytest.skip(_XDIST_SKIP_REASON)
    missing = unobserved_core(session_observed_problem_types)
    assert not missing, (
        f"§7 vocabulary entries never observed during the run: {sorted(missing)}"
    )
