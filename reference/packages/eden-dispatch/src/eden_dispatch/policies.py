"""Reference ideation policies for the orchestrator role.

An "ideation policy" is a ``Callable[[ExperimentStateView], int]``
that returns the number of new ideation tasks the orchestrator should
create on this iteration. The orchestrator invokes the policy once
per iteration when
``dispatch_mode.ideation_creation == "auto"``
([`03-roles.md`](../../../../spec/v0/03-roles.md) §6.2 / §6.4).

The reference policies here are selected via the experiment config's
``ideation_policy`` block ([`02-data-model.md`](../../../../spec/v0/02-data-model.md) §2.4):

- :func:`maintain_pending` is the default (also used when the
  ``ideation_policy`` block is absent) — a bounded-overshoot policy
  that keeps the pending-ideation queue at a target depth. Its
  multi-instance behavior is the §6.4 ``N * T`` overshoot bound; the
  reference accepts the overshoot.
- :func:`fixed_total` is a simple "create exactly N total ideation
  tasks across the experiment's lifetime, then stop" policy. Useful
  for hypothesis-testing experiments with a fixed budget.
"""

from __future__ import annotations

from collections.abc import Callable

from eden_contracts import (
    FixedTotalPolicyConfig,
    IdeationPolicyConfig,
    MaintainPendingPolicyConfig,
)

from .state_view import ExperimentStateView

IdeationPolicy = Callable[[ExperimentStateView], int]
"""Return type of every callable wired into the dispatch loop's
``ideation_policy`` parameter."""


def maintain_pending(
    target: int, *, max_total: int | None = None
) -> IdeationPolicy:
    """Keep the pending-ideation queue at ``target`` depth.

    The returned policy on each iteration computes
    ``max(0, target - pending_ideation_count)`` — i.e., "create enough
    to refill the queue to ``target``." When ``max_total`` is set, the
    policy also clamps so the experiment's total ideation count never
    exceeds ``max_total``; this is the safety ceiling for runaway
    loops described in plan §3.3.

    Args:
        target: Desired pending-queue depth. Each iteration tops up
            to this depth (subject to the ``max_total`` ceiling).
            MUST be a positive integer.
        max_total: Optional hard cap on lifetime ideation tasks. When
            ``state.total_ideation_count`` already meets or exceeds
            this value, the policy returns ``0`` regardless of pending
            depth. ``None`` disables the cap.

    Returns:
        A callable suitable for the dispatch loop's
        ``ideation_policy`` parameter.

    Raises:
        ValueError: ``target`` < 1 or ``max_total`` < 0.
    """
    if target < 1:
        msg = f"maintain_pending requires target >= 1 (got {target})"
        raise ValueError(msg)
    if max_total is not None and max_total < 0:
        msg = f"maintain_pending requires max_total >= 0 (got {max_total})"
        raise ValueError(msg)

    def _policy(state: ExperimentStateView) -> int:
        wanted = max(0, target - state.pending_ideation_count)
        if max_total is None:
            return wanted
        remaining = max(0, max_total - state.total_ideation_count)
        return min(wanted, remaining)

    return _policy


def fixed_total(total: int) -> IdeationPolicy:
    """Create exactly ``total`` ideation tasks across the experiment's lifetime.

    Equivalent to the pre-12a-2 ``--ideation-tasks N`` seed shape: the
    first iteration creates ``min(total, target_burst)``; subsequent
    iterations top up until ``state.total_ideation_count == total``;
    after that, the policy returns ``0`` forever.

    The default burst is ``total`` — i.e., the first iteration fires
    the entire seed at once, matching the pre-12a-2 behavior. A
    deployment that wants gentler ramp-up can compose with
    :func:`maintain_pending`'s ``max_total`` instead.

    Args:
        total: Hard cap on lifetime ideation tasks. MUST be >= 1.

    Returns:
        A callable suitable for the dispatch loop's
        ``ideation_policy`` parameter.
    """
    if total < 1:
        msg = f"fixed_total requires total >= 1 (got {total})"
        raise ValueError(msg)

    def _policy(state: ExperimentStateView) -> int:
        return max(0, total - state.total_ideation_count)

    return _policy


_DEFAULT_TARGET_PENDING = 3


def default_policy() -> IdeationPolicy:
    """Return the reference default ideation policy.

    Used when the experiment config's ``ideation_policy`` block is
    absent. Equivalent to
    ``maintain_pending(target=3, max_total=None)`` — the open-ended
    exploration shape that an experiment without an explicit budget
    expects.
    """
    return maintain_pending(target=_DEFAULT_TARGET_PENDING)


def build_policy(config: IdeationPolicyConfig | None) -> IdeationPolicy:
    """Materialize an :data:`IdeationPolicy` from an experiment-config block.

    When ``config`` is ``None`` (no ``ideation_policy`` block in the
    experiment config), returns :func:`default_policy`. Otherwise
    dispatches on ``config.kind`` and constructs the matching factory
    with the validated arguments from the config.

    Raises:
        ValueError: if the config's per-kind arguments are invalid
            (e.g., ``target < 1`` for ``maintain_pending``); the
            underlying factory's validation is the source of the
            error message.
    """
    if config is None:
        return default_policy()
    if isinstance(config, MaintainPendingPolicyConfig):
        return maintain_pending(target=config.target, max_total=config.max_total)
    if isinstance(config, FixedTotalPolicyConfig):
        return fixed_total(config.total)
    msg = f"unhandled ideation_policy kind: {config!r}"
    raise ValueError(msg)
