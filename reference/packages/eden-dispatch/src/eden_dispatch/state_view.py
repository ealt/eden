"""ExperimentStateView — read-only facade over Store counters.

The ``ExperimentStateView`` is what an :data:`IdeationPolicy` callable
receives. It exposes the small set of experiment-level counters that
existing reference policies consult; deployments needing more
information for a custom policy compose with ``Store.list_*`` directly.

Per the wave-4 plan (§6.2): "one read per counter; no caching —
simpler is right for now."
The builder runs at the start of each orchestrator iteration; counter
freshness is bounded by the iteration interval, which is the only
useful freshness for a policy that drives the next iteration.
"""

from __future__ import annotations

from dataclasses import dataclass

from eden_storage import Store


@dataclass(frozen=True)
class ExperimentStateView:
    """Counters the orchestrator's ideation-policy callable consults.

    Every field is a snapshot taken at view-construction time. The
    view does NOT hold a reference to the store; mutations after
    construction are NOT visible.

    Fields:

    - ``pending_ideation_count`` — ideation tasks currently in
      ``state == "pending"``. The ``maintain_pending`` reference
      policy compares this to its target.
    - ``in_flight_ideation_count`` — ideation tasks in ``state`` ∈
      ``{pending, claimed, submitted}`` (i.e. not yet terminal).
      The "live" set per [`03-roles.md`](../../../../spec/v0/03-roles.md)
      §6.4; useful for policies that want to bound the total
      outstanding load, not just the pending queue.
    - ``total_ideation_count`` — every ideation task that has ever
      existed (any state). Compared to a configured ``max_total``
      by safety-ceiling policies.
    - ``running_variant_count`` — variants in ``status == "starting"``.
      Variants that an executor has claimed but evaluation hasn't
      yet retired.
    - ``integrated_variant_count`` — variants in ``status ==
      "success"`` whose ``variant_commit_sha`` is set. Counts the
      durable, lineage-merged variants — what a "convergence" policy
      consults.
    """

    pending_ideation_count: int
    in_flight_ideation_count: int
    total_ideation_count: int
    running_variant_count: int
    integrated_variant_count: int


_LIVE_TASK_STATES: frozenset[str] = frozenset({"pending", "claimed", "submitted"})


def build_experiment_state_view(store: Store) -> ExperimentStateView:
    """Return a fresh :class:`ExperimentStateView` for ``store``.

    Implementation note: the function performs separate ``list_tasks``
    / ``list_variants`` calls per counter rather than reading once and
    deriving everything in-memory. For the reference scale (single-
    experiment, low-thousands of records) the wire cost is negligible
    and the read-per-counter shape stays simple. When the cost becomes
    load-bearing, a future iteration can replace this with a single
    composite read; the policy contract (``state: ExperimentStateView``)
    won't change.
    """
    all_ideation = store.list_tasks(kind="ideation")
    pending_ideation = sum(1 for t in all_ideation if t.state == "pending")
    in_flight_ideation = sum(
        1 for t in all_ideation if t.state in _LIVE_TASK_STATES
    )
    total_ideation = len(all_ideation)
    running_variants = len(store.list_variants(status="starting"))
    success_variants = store.list_variants(status="success")
    integrated_variants = sum(
        1 for v in success_variants if v.variant_commit_sha is not None
    )
    return ExperimentStateView(
        pending_ideation_count=pending_ideation,
        in_flight_ideation_count=in_flight_ideation,
        total_ideation_count=total_ideation,
        running_variant_count=running_variants,
        integrated_variant_count=integrated_variants,
    )
