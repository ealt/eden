"""Reference termination policies for the orchestrator role (12a-3).

A "termination policy" is a
``Callable[[ExperimentStateView], TerminationDecision]`` that the
orchestrator invokes once per iteration when
``dispatch_mode.termination == "auto"``
([`03-roles.md`](../../../../spec/v0/03-roles.md) §6.2 decision-type 0).
The decision is one of:

- :class:`Continue` — proceed to the four operational decisions for
  this iteration.
- :class:`Terminate(reason)` — atomically transition the experiment to
  ``"terminated"`` and append ``experiment.terminated`` with the
  ``reason`` string.

The reference policies in this module are importable via the
orchestrator service's ``--termination-policy <module:callable>`` flag.
Deployments that want different termination dynamics ship their own
callable matching :data:`TerminationPolicy` and point the flag at it.

The five reference policies are the spec-canonical re-implementations
of the four pre-12a-3 ``ExperimentConfig`` termination fields (now
removed from the normative schema) plus a never-terminate default:

- :func:`never_terminate` — default; explicit no-op.
- :func:`max_variants_policy` — ceiling on variants attempted.
- :func:`max_wall_time_policy` — wall-time deadline.
- :func:`convergence_window_policy` — N integrations without objective
  improvement.
- :func:`target_condition_policy` — latest variant's metric ≥ threshold.

Per [plan §3.4](../../../../docs/plans/eden-phase-12a-3-lifecycle-policy.md)
each reference policy matches the legacy field's semantics but is
non-normative: the spec defines only the
``Continue / Terminate(reason)`` contract.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from .state_view import ExperimentStateView

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Continue:
    """Termination-decision sentinel: proceed to operational decisions."""


@dataclass(frozen=True)
class Terminate:
    """Termination-decision result: commit ``running → terminated``."""

    reason: str


TerminationDecision = Continue | Terminate
"""Discriminated union of termination-policy outcomes."""

TerminationPolicy = Callable[[ExperimentStateView], TerminationDecision]
"""Termination-policy callable signature.

Invoked once per orchestrator iteration when the experiment is
``running`` and ``dispatch_mode.termination == "auto"`` per
``03-roles.md`` §6.2 decision-type 0.
"""

ObjectiveDirection = Literal["maximize", "minimize"]


def never_terminate(state: ExperimentStateView) -> TerminationDecision:  # noqa: ARG001
    """Default termination policy — always :class:`Continue`."""
    return Continue()


def max_variants_policy(target: int) -> TerminationPolicy:
    """Terminate when ``state.attempted_variant_count >= target``.

    Matches the pre-12a-3 ``max_variants`` config field's semantics:
    a ceiling on variants ATTEMPTED (any variant in any status), not on
    variants integrated.

    Args:
        target: Hard ceiling on attempted variants. MUST be >= 1.

    Raises:
        ValueError: ``target`` < 1.
    """
    if target < 1:
        msg = f"max_variants_policy requires target >= 1 (got {target})"
        raise ValueError(msg)

    def _policy(state: ExperimentStateView) -> TerminationDecision:
        if state.attempted_variant_count >= target:
            return Terminate(
                reason=f"max_variants={target} reached "
                f"(attempted={state.attempted_variant_count})"
            )
        return Continue()

    return _policy


def max_wall_time_policy(duration: timedelta) -> TerminationPolicy:
    """Terminate when wall-time since ``experiment.created_at`` exceeds ``duration``.

    Matches the pre-12a-3 ``max_wall_time`` config field's semantics.

    Args:
        duration: Maximum elapsed wall-time. MUST be positive.

    Raises:
        ValueError: ``duration`` is zero or negative.
    """
    if duration.total_seconds() <= 0:
        msg = f"max_wall_time_policy requires duration > 0 (got {duration!r})"
        raise ValueError(msg)

    def _policy(state: ExperimentStateView) -> TerminationDecision:
        created = _parse_iso_utc(state.experiment_created_at)
        elapsed = datetime.now(UTC) - created
        if elapsed >= duration:
            return Terminate(
                reason=f"max_wall_time={duration} reached "
                f"(elapsed={elapsed})"
            )
        return Continue()

    return _policy


def convergence_window_policy(
    metric: str,
    *,
    window: int,
    direction: ObjectiveDirection = "maximize",
) -> TerminationPolicy:
    """Terminate when ``metric`` has not improved in the last ``window`` integrations.

    Matches the pre-12a-3 ``convergence_window`` config field's
    semantics: walks the trailing ``window`` integrated variants and
    asks whether the best value in that window is the global best.
    When yes, no improvement is happening — terminate.

    Args:
        metric: Evaluation key to read from each integrated variant's
            ``evaluation`` dict. Variants whose evaluation lacks the
            key (or whose value is non-numeric or NaN) are skipped.
        window: Trailing window of integrated variants to consider.
            MUST be >= 1.
        direction: ``"maximize"`` (default) treats larger values as
            better; ``"minimize"`` treats smaller values as better.

    Raises:
        ValueError: ``window`` < 1 or ``direction`` is unknown.
    """
    if window < 1:
        msg = f"convergence_window_policy requires window >= 1 (got {window})"
        raise ValueError(msg)
    if direction not in ("maximize", "minimize"):
        msg = (
            f"convergence_window_policy direction must be "
            f"'maximize' or 'minimize' (got {direction!r})"
        )
        raise ValueError(msg)
    is_better: Callable[[float, float], bool] = (
        (lambda new, prev: new > prev)
        if direction == "maximize"
        else (lambda new, prev: new < prev)
    )

    def _policy(state: ExperimentStateView) -> TerminationDecision:
        values: list[float] = []
        for evaluation in state.recent_evaluations:
            value = _numeric_metric(evaluation, metric)
            if value is None:
                continue
            values.append(value)
        if len(values) < window:
            return Continue()
        # Walk forward; the trailing window's "best" must match the
        # all-time best for "no improvement" to hold.
        tail = values[-window:]
        head = values[:-window]
        global_best = _best(values, is_better)
        tail_best = _best(tail, is_better)
        if head:
            head_best = _best(head, is_better)
            # If the trailing window's best is no better than the
            # head's best, the window represents no improvement.
            if not is_better(tail_best, head_best):
                return Terminate(
                    reason=f"convergence: {metric!r} no improvement in last "
                    f"{window} integrations (best={global_best})"
                )
        # No head means we haven't seen anything before the window
        # boundary; can't yet say "no improvement."
        return Continue()

    return _policy


def target_condition_policy(
    metric: str, *, threshold: float, direction: ObjectiveDirection = "maximize"
) -> TerminationPolicy:
    """Terminate when the latest integrated variant's ``metric`` crosses ``threshold``.

    Matches the pre-12a-3 ``target_condition`` config field's
    semantics, narrowed to a single-metric comparison (the legacy
    field accepted a full expression; the reference policy keeps the
    surface simple — deployments that want richer predicates ship
    their own callable).

    Args:
        metric: Evaluation key to read from the latest integrated
            variant.
        threshold: Comparison threshold.
        direction: ``"maximize"`` (default) terminates when value
            >= threshold; ``"minimize"`` terminates when value <=
            threshold.

    Raises:
        ValueError: ``direction`` is unknown.
    """
    if direction not in ("maximize", "minimize"):
        msg = (
            f"target_condition_policy direction must be "
            f"'maximize' or 'minimize' (got {direction!r})"
        )
        raise ValueError(msg)
    is_met: Callable[[float], bool] = (
        (lambda value: value >= threshold)
        if direction == "maximize"
        else (lambda value: value <= threshold)
    )

    def _policy(state: ExperimentStateView) -> TerminationDecision:
        if state.latest_evaluation is None:
            return Continue()
        value = _numeric_metric(state.latest_evaluation, metric)
        if value is None:
            return Continue()
        if is_met(value):
            sign = ">=" if direction == "maximize" else "<="
            return Terminate(
                reason=f"target reached: {metric}={value} {sign} {threshold}"
            )
        return Continue()

    return _policy


# ----------------------------------------------------------------------
# Default policy + env-var-configured factories
# ----------------------------------------------------------------------


def default_termination_policy() -> TerminationPolicy:
    """Return the reference default termination policy.

    The reference orchestrator's ``--termination-policy`` flag defaults
    here. The default is :func:`never_terminate`, which keeps pre-12a-3
    deployments' runtime behavior unchanged (the legacy
    ``max_variants`` / ``max_wall_time`` fields are removed from the
    normative schema, and an empty ``--termination-policy`` setting
    matches the historical never-terminate behavior).

    Deployments that want one of the four configured policies wrap
    them via a small factory module:

        # my_policies.py
        from datetime import timedelta
        from eden_dispatch.termination import max_wall_time_policy

        def my_termination():
            return max_wall_time_policy(timedelta(hours=2))

    and pass ``--termination-policy my_policies:my_termination``.
    """
    return never_terminate


def env_max_variants_policy() -> TerminationPolicy:
    """``max_variants_policy(EDEN_TERMINATION_MAX_VARIANTS)``; raises if unset.

    Pre-12a-3 deployments that consumed the ``max_variants`` config
    field can point ``--termination-policy`` at this factory and set
    ``EDEN_TERMINATION_MAX_VARIANTS=<N>`` in the environment to recover
    the legacy semantics. The pre-12a-3 field is removed from the
    normative schema; this is the reference path for restoring its
    behavior.
    """
    raw = os.environ.get("EDEN_TERMINATION_MAX_VARIANTS")
    if not raw:
        msg = (
            "env_max_variants_policy requires EDEN_TERMINATION_MAX_VARIANTS "
            "to be set to a positive integer"
        )
        raise ValueError(msg)
    try:
        target = int(raw)
    except ValueError as exc:
        msg = (
            f"EDEN_TERMINATION_MAX_VARIANTS={raw!r} is not a valid integer"
        )
        raise ValueError(msg) from exc
    return max_variants_policy(target)


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------


def _parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp (trailing ``Z``) into an aware datetime."""
    # `datetime.fromisoformat` in 3.11+ accepts the trailing Z. Older
    # Pythons would need a manual swap; the repo targets 3.12.
    return datetime.fromisoformat(value)


def _numeric_metric(
    evaluation: dict[str, object], key: str
) -> float | None:
    """Extract a numeric metric value, or ``None`` if absent / non-numeric."""
    if key not in evaluation:
        return None
    value = evaluation[key]
    if isinstance(value, bool):
        # bool is an int subclass; reject explicitly per chapter 02
        # §1.3 metric-type discipline.
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        as_float = float(value)
        if math.isnan(as_float) or math.isinf(as_float):
            return None
        return as_float
    return None


def _best(values: list[float], is_better: Callable[[float, float], bool]) -> float:
    """Return the "best" of ``values`` per the comparator. ``values`` MUST be non-empty."""
    best = values[0]
    for v in values[1:]:
        if is_better(v, best):
            best = v
    return best


__all__ = [
    "Continue",
    "ObjectiveDirection",
    "Terminate",
    "TerminationDecision",
    "TerminationPolicy",
    "convergence_window_policy",
    "default_termination_policy",
    "env_max_variants_policy",
    "max_variants_policy",
    "max_wall_time_policy",
    "never_terminate",
    "target_condition_policy",
]
