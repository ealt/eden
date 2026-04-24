"""Evaluator worker host main loop."""

from __future__ import annotations

from eden_contracts import MetricsSchema
from eden_dispatch import ScriptedEvaluator
from eden_service_common import StopFlag, get_logger, make_evaluate_fn
from eden_storage import Store

log = get_logger(__name__)


def run_evaluator_loop(
    *,
    store: Store,
    worker_id: str,
    metrics_schema: MetricsSchema,
    fail_every: int | None,
    poll_interval: float,
    stop: StopFlag,
) -> None:
    """Poll for pending evaluate tasks and drive each through the scripted profile.

    Returns only when ``stop`` is set.
    """
    evaluator = ScriptedEvaluator(
        worker_id=worker_id,
        evaluate_fn=make_evaluate_fn(
            metrics_schema=metrics_schema,
            fail_every=fail_every,
        ),
    )
    while not stop.is_set():
        processed = evaluator.run_pending(store, stop=stop.is_set)
        if processed == 0 and stop.wait(poll_interval):
            return
