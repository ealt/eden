"""Reference ideation policies for the orchestrator role.

# noqa: ERA001  (the env-var defaults below appear in plan §5.7's
# `.env.example` table; the policies module exposes the read so the
# CLI flag default can stay a plain ``module:callable`` string.)

An "ideation policy" is a ``Callable[[ExperimentStateView], int]``
that returns the number of new ideation tasks the orchestrator should
create on this iteration. The orchestrator invokes the policy once
per iteration when
``dispatch_mode.ideation_creation == "auto"``
([`03-roles.md`](../../../../spec/v0/03-roles.md) §6.2 / §6.4).

The reference policies here are importable via the orchestrator
service's ``--ideation-policy <module:callable>`` flag. Deployments
that want different ideation dynamics ship their own callable matching
:data:`IdeationPolicy` and point the flag at it.

Per [plan §3.3](../../../../docs/plans/eden-phase-12a-2-orchestrator-as-role.md):

- :func:`maintain_pending` is the default — a bounded-overshoot policy
  that keeps the pending-ideation queue at a target depth. Its
  multi-instance behavior is the §6.4 ``N * T`` overshoot bound; the
  reference accepts the overshoot.
- :func:`fixed_total` is a simple "create exactly N total ideation
  tasks across the experiment's lifetime, then stop" policy. Useful
  for the original static-seed shape that the wave-4 CLI rework
  replaces — operators who actually want a one-shot seed still have
  a clear path.
"""

from __future__ import annotations

import os
from collections.abc import Callable

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
    """Return the reference ``maintain_pending`` policy with env-var configuration.

    The reference orchestrator's ``--ideation-policy`` default points
    here. Configuration is read from environment variables so the
    operator can adjust shape without rewriting the policy factory:

    - ``EDEN_IDEATION_POLICY_TARGET_PENDING`` — target queue depth
      (default ``3``; matches the pre-12a-2 ``EDEN_IDEATE_TASKS=3``
      seed shape).
    - ``EDEN_IDEATION_POLICY_MAX_TOTAL`` — hard cap on lifetime
      ideation-task count (default unset → unbounded). Setting this
      converts the policy from "continuous" to "bounded total" — once
      the cap is reached the orchestrator stops creating ideation
      tasks regardless of pending depth. Useful as a safety ceiling
      for runaway loops and as the wave-4 e2e-test substitute for the
      retired ``--ideation-tasks N`` static seed.

    Invalid values raise ``ValueError`` from the underlying
    :func:`maintain_pending` factory; the orchestrator's CLI surfaces
    that as a startup failure so the operator sees the misconfiguration
    immediately rather than discovering it iterations later.
    """
    target_pending = _int_env(
        "EDEN_IDEATION_POLICY_TARGET_PENDING", _DEFAULT_TARGET_PENDING
    )
    max_total = _optional_int_env("EDEN_IDEATION_POLICY_MAX_TOTAL")
    return maintain_pending(target=target_pending, max_total=max_total)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        msg = f"{name}={raw!r} is not a valid integer"
        raise ValueError(msg) from exc


def _optional_int_env(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        msg = f"{name}={raw!r} is not a valid integer"
        raise ValueError(msg) from exc
